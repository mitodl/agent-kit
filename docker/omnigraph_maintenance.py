#!/usr/bin/env python3
"""Nightly maintenance window for the omnigraph data tier (omnigraph 0.13).

`omnigraph optimize` and `cleanup` are direct writers of the storage root.
From 0.12 on a direct writer is refused with `state_lock_held` while the
server holds its `serve` lock, and there are no HTTP maintenance routes, so
maintenance needs the server stopped. This command owns that window:

    skip if a migration is armed or the Deployment is already at 0 replicas
    take the unlock Lease, waiting out a server that has just booted
    scale the server Deployment to 0
    wait until no live pod matches the Deployment's own selector
    status  -> force-unlock the `serve` lock; any other lock: stop
    per graph: optimize, force-unlock
    on the cleanup day, per graph: cleanup, force-unlock
    restore the replica count, release the Lease

The last line also runs when the window is cut short: by SIGTERM (the Job's
`activeDeadlineSeconds`, a drain), by a failed step, or by the window's own
deadline. A running command is stopped first, and the lock it left is removed
only while the Lease is still confirmed. After SIGTERM that takes up to 30s
to stop the command, then a status, an unlock and the scale-up, so the pod
needs a `terminationGracePeriodSeconds` of 120, not the default 30.

The exit status is 0 only if every graph was maintained and the window was
closed: the root unlocked, and the server either scaled back up or left down
for a migration.

WHAT IS RESTORED, AND WHEN IT IS NOT

- The replica count is restored only if the Deployment is still at 0 and does
  not carry the migration-armed annotation. Arming a storage-format migration
  sets `replicas: 0` and the annotation in one update, and nothing reconciles
  the count afterwards, so an unconditional restore would start a server under
  an armed migration. The write is conditional on the Deployment's
  `resourceVersion`, so an arm that lands between the read and the write is
  seen on the retry.
- If this window's own lock could not be removed, or the lock's state could
  not be read (the Lease was lost, `cluster status` or `force-unlock` failed),
  the Deployment stays at 0. A server started on that root would refuse the
  `graph_operation` lock on every boot. The council probe's first run after
  the window is the alert, and the runbook clears the lock and scales back up.

Design: docs/internals/design/omnigraph-0-13-upgrade-spec.md (D5). The Lease,
the pod check and the CLI wrappers are the server entrypoint's
(omnigraph_server_entrypoint.py), which this is installed beside.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import omnigraph_server_entrypoint as handoff
import yaml
from omnigraph_server_entrypoint import (
    BootRefused,
    Cluster,
    CommandTimedOut,
    KubernetesApi,
    KubernetesError,
    UnlockLease,
    log,
)

MIGRATION_ARMED_ANNOTATION = "ol.mit.edu/migration-armed"

# A server that has just booted keeps the Lease until it expires, up to
# LEASE_DURATION_SECONDS plus the clock-skew margin after its last renewal.
LEASE_WAIT_SECONDS = 90
LEASE_RETRY_SECONDS = 5
# terminationGracePeriodSeconds (35) plus the time the kubelet and the
# Deployment controller take to act on the scale-down.
DEFAULT_SERVER_STOP_SECONDS = 90
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600
RESTORE_ATTEMPTS = 10


class WindowTerminated(Exception):
    """SIGTERM arrived and the window has to close now."""


@dataclass(frozen=True)
class Settings:
    """What the CronJob supplies through the environment.

    :param cleanup_older_than: the retention passed to `cleanup --older-than`.
    :param cleanup_iso_weekday: the UTC day `cleanup` also runs on (1 is
        Monday, 7 is Sunday), or ``None`` to never run it.
    :param window_seconds: how long after the start a new command may still
        begin. The Job's ``activeDeadlineSeconds`` has to be longer, by one
        command's timeout and the restore.
    """

    config_dir: Path
    deployment: str
    lease_name: str
    pod_name: str
    cleanup_older_than: str
    cleanup_iso_weekday: int | None
    window_seconds: float
    command_timeout_seconds: float
    server_stop_seconds: float

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Settings:
        weekday = env.get("OMNIGRAPH_CLEANUP_ISO_WEEKDAY")
        return cls(
            config_dir=Path(
                env.get("OMNIGRAPH_CLUSTER_DIR", handoff.DEFAULT_CLUSTER_DIR)
            ),
            deployment=env["OMNIGRAPH_SERVER_DEPLOYMENT"],
            lease_name=env["OMNIGRAPH_UNLOCK_LEASE"],
            pod_name=env.get("POD_NAME") or env["HOSTNAME"],
            cleanup_older_than=env["OMNIGRAPH_CLEANUP_OLDER_THAN"],
            cleanup_iso_weekday=int(weekday) if weekday else None,
            window_seconds=float(env["OMNIGRAPH_WINDOW_SECONDS"]),
            command_timeout_seconds=float(
                env.get(
                    "OMNIGRAPH_COMMAND_TIMEOUT_SECONDS",
                    DEFAULT_COMMAND_TIMEOUT_SECONDS,
                )
            ),
            server_stop_seconds=float(
                env.get("OMNIGRAPH_SERVER_STOP_SECONDS", DEFAULT_SERVER_STOP_SECONDS)
            ),
        )


def cluster_layout(config_dir: Path) -> tuple[str, list[str]]:
    """The storage root and graph ids `cluster.yaml` declares.

    Maintenance addresses a graph as `--cluster <root> --graph <id>`. A config
    directory is accepted there only when the root is that directory, which is
    the case when `cluster.yaml` names no `storage`.
    """
    declared = yaml.safe_load((config_dir / "cluster.yaml").read_text())
    return declared.get("storage") or str(config_dir), list(declared["graphs"])


def migration_armed(deployment: dict[str, Any]) -> bool:
    annotations = deployment["metadata"].get("annotations") or {}
    return MIGRATION_ARMED_ANNOTATION in annotations


def pod_selector(deployment: dict[str, Any]) -> str:
    """The label selector of the Deployment's own pods.

    Read from the Deployment and not configured beside it: a selector that
    matched no pod would show the server as stopped while it was still
    running, and its `serve` lock would be removed under it.

    :raises BootRefused: on a selector this cannot render exactly.
    """
    selector = deployment["spec"]["selector"]
    labels = selector.get("matchLabels")
    if not labels or selector.get("matchExpressions"):
        msg = (
            f"cannot tell which pods are {deployment['metadata']['name']}'s from "
            f"its selector {selector!r} (matchLabels only)"
        )
        raise BootRefused(msg)
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


class Window:
    """One stop window: the server is down from ``open`` until ``close``.

    :param deployment: the server Deployment as read at the start, with a
        non-zero replica count and no migration armed.
    """

    def __init__(
        self,
        api: KubernetesApi,
        lease: UnlockLease,
        settings: Settings,
        deployment: dict[str, Any],
    ) -> None:
        self._api = api
        self._lease = lease
        self._settings = settings
        self._replicas = deployment["spec"]["replicas"]
        self._resource_version = deployment["metadata"]["resourceVersion"]
        self._selector = pod_selector(deployment)
        self._cluster = Cluster(settings.config_dir, None, lease)
        self._root, self._graphs = cluster_layout(settings.config_dir)
        self._deadline = time.monotonic() + settings.window_seconds
        self._terminated = threading.Event()
        self._may_have_stopped_server = False
        self._ran_command = False
        self.failed: list[str] = []

    def terminate(self) -> None:
        """Ask the window to close. Safe to call from a signal handler."""
        self._terminated.set()

    def _check_terminated(self) -> None:
        if self._terminated.is_set():
            raise WindowTerminated

    def take_lease(self) -> None:
        """Take the unlock Lease, waiting for a recent holder's to expire.

        A server that booted in the last minute still holds it, and the hourly
        token-sync restart can fall right before a window.

        :raises BootRefused: if it is still held after ``LEASE_WAIT_SECONDS``.
        """
        deadline = time.monotonic() + LEASE_WAIT_SECONDS
        while True:
            self._check_terminated()
            try:
                self._lease.acquire()
            except BootRefused as refusal:
                if time.monotonic() >= deadline:
                    raise
                log(f"waiting for the Lease: {refusal}")
                time.sleep(LEASE_RETRY_SECONDS)
            else:
                return

    def open(self) -> None:
        """Stop the server and take over the lock it leaves.

        The scale-down is conditional on the Deployment's ``resourceVersion``
        as read when the replica count was recorded, so a change since then
        (an armed migration) refuses it instead of being overwritten.
        """
        settings = self._settings
        self._check_terminated()
        # Before the request: one that is applied but whose response is lost
        # has stopped the server all the same, and `close` has to restore it.
        self._may_have_stopped_server = True
        try:
            self._api.replace_scale(settings.deployment, 0, self._resource_version)
        except KubernetesError as exc:
            msg = f"could not scale {settings.deployment} to 0: {exc}"
            raise BootRefused(msg) from exc
        log(f"scaled {settings.deployment} from {self._replicas} to 0")
        self._wait_for_server_to_stop()
        lock = self._cluster.lock()
        if not lock["locked"]:
            return
        if lock["lock_operation"] != "serve":
            # A `deployment` lock is an interrupted boot's, which the server
            # entrypoint reconciles on its next start. A `graph_operation`
            # lock is an earlier window's or an operator's, and the runbook's.
            msg = (
                f"the cluster is locked by a {lock['lock_operation']!r} operation "
                f"(lock id {lock['lock_id']}), not by the server that just stopped"
            )
            raise BootRefused(msg)
        log("releasing the serve lock left by the stopped server")
        self._cluster.force_unlock(lock["lock_id"])

    def _wait_for_server_to_stop(self) -> None:
        """Return once no live pod matches the server Deployment's selector.

        :raises BootRefused: if one is still live after the configured wait,
            or the Lease is lost.
        """
        wait_seconds = self._settings.server_stop_seconds
        deadline = time.monotonic() + wait_seconds
        while True:
            self._check_terminated()
            if self._lease.lost():
                msg = "lost the unlock Lease while waiting for the server to stop"
                raise BootRefused(msg)
            try:
                live = handoff.live_pod_names(self._api.list_pods(self._selector))
            except KubernetesError as exc:
                msg = f"cannot list pods matching {self._selector!r}: {exc}"
                raise BootRefused(msg) from exc
            if not live:
                return
            if time.monotonic() >= deadline:
                msg = f"server pods are still live after {wait_seconds:.0f}s: {live}"
                raise BootRefused(msg)
            log(f"waiting for server pods to exit: {live}")
            time.sleep(handoff.PEER_POLL_SECONDS)

    def run(self, *, cleanup: bool) -> None:
        """Maintain every graph, carrying on past one that fails.

        One unopenable graph would otherwise cost every graph after it its
        maintenance. The failures are collected in ``failed``.

        Every graph is optimized before any is cleaned, and the cleanup pass
        starts one graph further along each ISO week. A window that runs out
        then delays a graph's cleanup to a later week, where a fixed order
        would skip the same graphs at the end of the list every time.
        """
        for graph in self._graphs:
            self._maintain(graph, "optimize")
        if not cleanup:
            return
        first = datetime.now(UTC).isocalendar().week % len(self._graphs)
        for graph in self._graphs[first:] + self._graphs[:first]:
            # `--yes` because an s3:// root is a non-local scope, and a
            # destructive write there refuses without a TTY.
            self._maintain(
                graph,
                "cleanup",
                "--older-than",
                self._settings.cleanup_older_than,
                "--confirm",
                "--yes",
            )

    def _maintain(self, graph: str, command: str, *args: str) -> None:
        step = f"{command} {graph}"
        self._check_terminated()
        if time.monotonic() >= self._deadline:
            log(f"skipping {step}: the window is over")
            self.failed.append(step)
            return
        log(f"=== omnigraph {step}")
        self._ran_command = True
        try:
            result = handoff.run_omnigraph(
                [command, "--cluster", self._root, "--graph", graph, *args],
                self._lease,
                timeout_seconds=self._settings.command_timeout_seconds,
                interrupt=self._check_terminated,
            )
        except CommandTimedOut as exc:
            log(f"!!! {exc}")
            self.failed.append(step)
        else:
            sys.stdout.write(result.stdout)
            if result.returncode != 0:
                log(f"!!! omnigraph {step} failed (exit {result.returncode})")
                self.failed.append(step)
        # A command that fails or is stopped keeps its lock too.
        self._release_own_lock()

    def _release_own_lock(self) -> None:
        """Remove the `graph_operation` lock a command of this window left.

        Only once this window has started a command. The root was unlocked
        before that command started, the Lease excludes every other unlocker,
        and no server pod is live, so a `graph_operation` lock found then is
        that command's. One found before any command ran is somebody else's.
        A `serve` lock is a server's that this window did not see stop.
        """
        if not self._ran_command:
            return
        lock = self._cluster.lock()
        if lock["locked"] and lock["lock_operation"] == "graph_operation":
            self._cluster.force_unlock(lock["lock_id"])

    def close(self) -> bool:
        """Leave the root unlocked and the server running again.

        :returns: whether the window was closed: its own lock is gone, and the
            server is scaled back up or deliberately left down for a
            migration. ``False`` leaves the Deployment for the runbook.
        """
        if not self._may_have_stopped_server:
            return True
        try:
            self._release_own_lock()
        except Exception as exc:  # noqa: BLE001
            # Whatever went wrong, the lock's state is unknown, and a server
            # started on a root this window left locked cannot boot.
            log(
                f"!!! {exc}; leaving {self._settings.deployment} at 0 replicas "
                "(runbook: clear the lock, then scale up)"
            )
            return False
        return self._restore()

    def _restore(self) -> bool:
        name = self._settings.deployment
        for _ in range(RESTORE_ATTEMPTS):
            try:
                deployment = self._api.get_deployment(name)
                if migration_armed(deployment):
                    log(f"a migration was armed during the window; leaving {name} at 0")
                    return True
                current = deployment["spec"].get("replicas")
                if current != 0:
                    log(f"{name} is at {current} replicas; nothing to restore")
                    return True
                self._api.replace_scale(
                    name, self._replicas, deployment["metadata"]["resourceVersion"]
                )
            except KubernetesError as exc:
                log(f"could not restore {name}: {exc}")
                time.sleep(handoff.PEER_POLL_SECONDS)
            else:
                log(f"restored {name} to {self._replicas} replicas")
                return True
        log(f"!!! {name} is still at 0 replicas (runbook: scale it back up)")
        return False


def main(env: dict[str, str]) -> int:
    handoff.LOG_PREFIX = "omnigraph-maintenance"
    settings = Settings.from_env(env)
    api = KubernetesApi.in_cluster()
    deployment = api.get_deployment(settings.deployment)
    if migration_armed(deployment):
        log("a storage-format migration is armed; skipping this window")
        return 0
    if not deployment["spec"].get("replicas"):
        log(f"{settings.deployment} is at 0 replicas; skipping this window")
        return 0
    cleanup = datetime.now(UTC).isoweekday() == settings.cleanup_iso_weekday

    lease = UnlockLease(api, settings.lease_name, settings.pod_name)
    try:
        window = Window(api, lease, settings, deployment)
    except BootRefused as refusal:
        log(f"!!! not opening a window: {refusal}")
        return 1
    # The handler only sets a flag, which the window checks between steps and
    # once a second while a command runs. Nothing is interrupted mid-call, and
    # the close below runs to its end whenever the signal arrives.
    previous_handler = signal.signal(
        signal.SIGTERM, lambda signum, frame: window.terminate()
    )
    cut_short = False
    try:
        window.take_lease()
        window.open()
        window.run(cleanup=cleanup)
    except BootRefused as refusal:
        log(f"!!! window cut short: {refusal}")
        cut_short = True
    except WindowTerminated:
        log("!!! SIGTERM: closing the window")
        cut_short = True
    finally:
        try:
            closed = window.close()
        finally:
            lease.release()
            signal.signal(signal.SIGTERM, previous_handler)
    if window.failed:
        log(f"!!! failed or skipped: {', '.join(window.failed)}")
    if cut_short or not closed or window.failed:
        return 1
    log("maintenance window complete: all graphs maintained")
    return 0


if __name__ == "__main__":
    sys.exit(main(dict(os.environ)))
