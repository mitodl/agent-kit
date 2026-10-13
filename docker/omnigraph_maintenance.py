#!/usr/bin/env python3
"""Nightly maintenance window for the omnigraph data tier (omnigraph 0.13).

`omnigraph optimize` and `cleanup` are direct writers of the storage root.
From 0.12 on a direct writer is refused with `state_lock_held` while the
server holds its `serve` lock, and there are no HTTP maintenance routes, so
maintenance needs the server stopped. This command owns that window:

    skip if a migration is armed or the Deployment is already at 0 replicas
    take the unlock Lease                              (else exit 1)
    scale the server Deployment to 0
    wait until no live pod matches its selector
    status  -> force-unlock the `serve` lock; any other lock: stop
    per graph: optimize, force-unlock; on the cleanup day: cleanup, force-unlock
    restore the replica count, release the Lease

The last line also runs when the window is cut short: by SIGTERM (the Job's
`activeDeadlineSeconds`, a drain), by a failed step, or by the window's own
deadline. A running command is stopped first, and the lock it left is removed
only while the Lease is still confirmed.

WHAT IS RESTORED, AND WHEN IT IS NOT

- The replica count is restored only if the Deployment is still at 0 and does
  not carry the migration-armed annotation. Arming a storage-format migration
  sets `replicas: 0` and the annotation in one update, and nothing reconciles
  the count afterwards, so an unconditional restore would start a server under
  an armed migration. The write is conditional on the Deployment's
  `resourceVersion`, so an arm that lands between the read and the write is
  seen on the retry.
- If this window's own lock could not be removed (the Lease was lost, or
  `force-unlock` failed), the Deployment stays at 0. A server started on that
  root would refuse the `graph_operation` lock on every boot. The council
  probe's first run after the window is the alert, and the runbook clears the
  lock and scales back up.

Design: docs/internals/design/omnigraph-0-13-upgrade-spec.md (D5). The Lease,
the pod check and the CLI wrappers are the server entrypoint's
(omnigraph_server_entrypoint.py), which this is installed beside.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

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

# terminationGracePeriodSeconds (35) plus the time the kubelet and the
# Deployment controller take to act on the scale-down.
DEFAULT_SERVER_STOP_SECONDS = 90
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600
RESTORE_ATTEMPTS = 5


class WindowTerminated(BaseException):
    """SIGTERM arrived. Not an ``Exception``, so no step can swallow it."""


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
    pod_selector: str
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
            pod_selector=env["OMNIGRAPH_POD_SELECTOR"],
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


def wait_for_server_to_stop(
    api: KubernetesApi, selector: str, lease: UnlockLease, wait_seconds: float
) -> None:
    """Return once no live pod matches the server Deployment's selector.

    :raises BootRefused: if one is still live after ``wait_seconds``, or the
        Lease is lost.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        if lease.lost():
            msg = "lost the unlock Lease while waiting for the server to stop"
            raise BootRefused(msg)
        try:
            live = handoff.live_pod_names(api.list_pods(selector))
        except KubernetesError as exc:
            msg = f"cannot list pods matching {selector!r}: {exc}"
            raise BootRefused(msg) from exc
        if not live:
            return
        if time.monotonic() >= deadline:
            msg = f"server pods are still live after {wait_seconds:.0f}s: {live}"
            raise BootRefused(msg)
        log(f"waiting for server pods to exit: {live}")
        time.sleep(handoff.PEER_POLL_SECONDS)


class Window:
    """One stop window: the server is down from ``open`` until ``close``."""

    def __init__(
        self,
        api: KubernetesApi,
        lease: UnlockLease,
        settings: Settings,
        replicas: int,
    ) -> None:
        self._api = api
        self._lease = lease
        self._settings = settings
        self._replicas = replicas
        self._cluster = Cluster(settings.config_dir, None, lease)
        self._root, self._graphs = cluster_layout(settings.config_dir)
        self._deadline = time.monotonic() + settings.window_seconds
        self._stopped_server = False
        self.failed: list[str] = []

    def open(self, resource_version: str) -> None:
        """Stop the server and take over the lock it leaves.

        :param resource_version: the Deployment's, as read when the replica
            count was recorded, so a change since then (an armed migration)
            refuses the scale-down instead of being overwritten.
        """
        settings = self._settings
        try:
            self._api.replace_scale(settings.deployment, 0, resource_version)
        except KubernetesError as exc:
            msg = f"could not scale {settings.deployment} to 0: {exc}"
            raise BootRefused(msg) from exc
        self._stopped_server = True
        log(f"scaled {settings.deployment} from {self._replicas} to 0")
        wait_for_server_to_stop(
            self._api, settings.pod_selector, self._lease, settings.server_stop_seconds
        )
        lock = self._cluster.lock()
        if not lock["locked"]:
            return
        if lock["lock_operation"] != "serve":
            # A `deployment` lock is an interrupted boot's, which the server
            # entrypoint reconciles on its next start. A `graph_operation`
            # lock is an earlier window's, and the runbook's.
            msg = (
                f"the cluster is locked by a {lock['lock_operation']!r} operation "
                f"(lock id {lock['lock_id']}), not by the server that just stopped"
            )
            raise BootRefused(msg)
        log("releasing the serve lock left by the stopped server")
        self._cluster.force_unlock(lock["lock_id"])

    def run(self, *, cleanup: bool) -> None:
        """Maintain every graph, carrying on past one that fails.

        One unopenable graph would otherwise cost every graph after it its
        maintenance. The failures are collected in ``failed``.
        """
        for graph in self._graphs:
            self._maintain(graph, "optimize")
            if cleanup:
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
        if time.monotonic() >= self._deadline:
            log(f"skipping {step}: the window is over")
            self.failed.append(step)
            return
        log(f"=== omnigraph {step}")
        try:
            result = handoff.run_omnigraph(
                [command, "--cluster", self._root, "--graph", graph, *args],
                self._lease,
                timeout_seconds=self._settings.command_timeout_seconds,
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

        The root was unlocked before the command started, the Lease excludes
        every other unlocker, and no server pod is live, so a
        `graph_operation` lock found now is that command's. A `serve` lock is
        a server's that this window did not see stop, and stays.
        """
        lock = self._cluster.lock()
        if lock["locked"] and lock["lock_operation"] == "graph_operation":
            self._cluster.force_unlock(lock["lock_id"])

    def close(self) -> bool:
        """Leave the root unlocked and the server running again.

        :returns: whether the root was left without this window's lock. When
            it was not, the Deployment stays at 0.
        """
        if not self._stopped_server:
            return True
        try:
            self._release_own_lock()
        except BootRefused as refusal:
            log(
                f"!!! {refusal}; leaving {self._settings.deployment} at 0 replicas "
                "(runbook: clear the lock, then scale up)"
            )
            return False
        self._restore()
        return True

    def _restore(self) -> None:
        name = self._settings.deployment
        for _ in range(RESTORE_ATTEMPTS):
            try:
                deployment = self._api.get_deployment(name)
                if migration_armed(deployment):
                    log(f"a migration was armed during the window; leaving {name} at 0")
                    return
                current = deployment["spec"].get("replicas")
                if current != 0:
                    log(f"{name} is already at {current} replicas; not restoring")
                    return
                self._api.replace_scale(
                    name, self._replicas, deployment["metadata"]["resourceVersion"]
                )
            except KubernetesError as exc:
                log(f"could not restore {name}: {exc}")
                time.sleep(handoff.PEER_POLL_SECONDS)
            else:
                log(f"restored {name} to {self._replicas} replicas")
                return
        log(f"!!! {name} is still at 0 replicas (runbook: scale it back up)")


def _ignore_sigterm(signum: int, frame: object) -> None:
    """Let the close of the window finish.

    A handler and not ``SIG_IGN``, which the commands the close still runs
    would inherit, so that they could no longer be stopped.
    """


def _terminate(signum: int, frame: object) -> NoReturn:
    signal.signal(signal.SIGTERM, _ignore_sigterm)
    raise WindowTerminated


def main(env: dict[str, str]) -> int:
    handoff.LOG_PREFIX = "omnigraph-maintenance"
    settings = Settings.from_env(env)
    api = KubernetesApi.in_cluster()
    deployment = api.get_deployment(settings.deployment)
    replicas = deployment["spec"].get("replicas")
    if migration_armed(deployment):
        log("a storage-format migration is armed; skipping this window")
        return 0
    if not replicas:
        log(f"{settings.deployment} is at 0 replicas; skipping this window")
        return 0
    cleanup = datetime.now(UTC).isoweekday() == settings.cleanup_iso_weekday

    lease = UnlockLease(api, settings.lease_name, settings.pod_name)
    window = Window(api, lease, settings, replicas)
    interrupted = False
    previous_handler = signal.signal(signal.SIGTERM, _terminate)
    try:
        lease.acquire()
        window.open(deployment["metadata"]["resourceVersion"])
        window.run(cleanup=cleanup)
    except BootRefused as refusal:
        log(f"!!! window cut short: {refusal}")
        interrupted = True
    except WindowTerminated:
        log("!!! SIGTERM: closing the window")
        interrupted = True
    finally:
        signal.signal(signal.SIGTERM, _ignore_sigterm)
        unlocked = window.close()
        lease.release()
        signal.signal(signal.SIGTERM, previous_handler)
    if window.failed:
        log(f"!!! failed or skipped: {', '.join(window.failed)}")
    if interrupted or not unlocked or window.failed:
        return 1
    log("maintenance window complete: all graphs maintained")
    return 0


if __name__ == "__main__":
    sys.exit(main(dict(os.environ)))
