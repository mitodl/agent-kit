#!/usr/bin/env python3
"""Entrypoint for the omnigraph-server data-tier image (omnigraph 0.13 to 0.15).

omnigraph-server serves the graphs that `omnigraph cluster apply` has created
under the storage root and never creates them itself, so this converges the
cluster in the container's own start path before exec'ing the server. A direct
apply on an empty root creates the ledger and the graphs; on a converged root
it is a no-op.

WHY THIS IS NOT A SHELL SCRIPT ANY MORE

From 0.12 on, every writer of a cluster root persists an exclusive lock at
`<root>/__cluster/lock.json` and keeps it after it exits: the server leaves
`serve` (on 0.13 also after a clean SIGTERM, and after a start that fails), a
direct `cluster apply` leaves `deployment` (a no-op one too), `optimize`
and `cleanup` leave `graph_operation`. The next writer is refused with
`state_lock_held` until someone runs `cluster force-unlock <id>`. 0.14 releases
a server's lock on a clean shutdown, and on S3 a restarted server takes over a
crashed one's lock once its lease has gone silent, but only a `serve`
acquisition does that: the direct apply below still refuses the lock, and CLI
writers still keep theirs (omnigraph v0.14.0
`crates/omnigraph-cluster/src/state_lock.rs:74-87,336-343`,
`crates/omnigraph-server/src/lib.rs:4004-4029`). This pod restarts on its own (hourly
token-sync, config-hash rollouts, liveness kills, evictions, drains), so the
unlock has to happen here, and it is only safe once the previous owner's I/O
is over. The lock records a pid and no host, so that evidence comes from
Kubernetes:

1. One Lease (`coordination.k8s.io`) excludes every other unlocker: this
   entrypoint in another pod, the maintenance job, an operator following the
   runbook. Upstream's `force-unlock` reads, compares and deletes without a
   condition, so two concurrent unlockers can remove a lock the other one's
   child just took.
2. The pod list for the Deployment's selector shows whether another server pod
   is still alive. `strategy: Recreate` orders pods only during a rollout; on
   a delete, an eviction or a drain the replacement starts while the old pod
   is still Terminating and may still be committing.

The Lease is valid only while it is renewed, so every unlock re-confirms it
first, and losing it stops the running child and exits without unlocking.

BOOT SEQUENCE

    take the Lease                                  (else exit 1)
    wait until no other live pod matches the selector  (else exit 1)
    render policy group membership from the token map
    plan    -> exit 1 on an outstanding deployment, on a graph removal that
               is not allow-listed, or on any other plan error, except a
               schema change refused only because graphs have non-main
               branches
    lock    -> serve / deployment lock: force-unlock; anything else: exit 1
    if that schema change is pending:
        serve the root on loopback, list the blocked graphs' branches
            -> exit 1 if a graph not allow-listed has any
        per non-main branch: delete it through the loopback server
                             (SIGTERM: stop, exit 1; the next boot carries on)
        stop the loopback server, force-unlock a serve lock it left
        plan again -> exit 1 on any plan error
        SIGTERM by now (after the drop, or during this plan): exit 1
    apply
    lock    -> force-unlock the deployment lock apply left
    exec omnigraph-server

Design and the binary-verified facts behind each step:
docs/internals/design/omnigraph-0-13-upgrade-spec.md (D3, D4) and
docs/internals/design/omnigraph-0-14-upgrade-spec.md (D3).

TWO CLI DIALECTS

The image ships 0.15, but the CLI's own `version --json` still picks the
argument shapes (`cli_speaks_targets`), so the 0.13 path keeps working for an
image built with an overridden pin (on a local root: before 0.15 the CLI
refuses an inline `s3://` one). 0.14 and later take the destination
as `--target <root>` and `--config` as source only, and their `cluster status`
reports no lock operation, so a lock is read from `cluster plan`'s
`state_observations` there. The root is `cluster.yaml`'s `storage`; the CLI
accepts it as an inline `s3://` target from 0.15 on (ModernRelay/omnigraph#1189),
and reads the region from `AWS_REGION` or `AWS_DEFAULT_REGION`.

A 0.15 server that loses the S3 cluster lock's lease stops writing and exits
with code 2 (upstream #1221). Nothing here handles that specially: the server
is exec'd, so the code is the container's, and the restart boots through the
Lease and the pod check above like any other.

BRANCH VIEWS AND SCHEMA CHANGES

omnigraph 0.15 refuses a schema change on a graph that has any branch but
`main`. The code graphs always carry branch views, one per writer and git
branch (mcp/servers/witan-code/docs/BRANCH_INDEXING.md), so every change to
their schema would stall the deploy. Those views are caches the next branch
index rebuilds, so they are deleted before the apply, on the graphs
OMNIGRAPH_BRANCH_DROP_ALLOWLIST names and only when the plan is refused for
that reason alone. The council graph is never listed: its branches may hold
what people wrote.

The 0.15 CLI reads and deletes a graph's branches only on a local root or
through a server: on an inline `s3://` root `branch list`, `branch delete` and
`schema plan` all refuse with "inline access supports local storage only"
(Production, 2026-11-16), and it sends a token only over HTTPS. So the views
go through the HTTP API as OMNIGRAPH_VIEW_DROP_ACTOR, the actor the code-graph
policies grant `branch_delete` (witan-ci, the stale-view reaper's). The
pre-deploy gate deletes them through the serving pod, so a large drop (1006
views took 25 minutes in Production) happens while the old pod still serves.
The boot then finds only views written since the gate ran, and deletes them
through an `omnigraph-server` it runs on loopback against the root, with no
other server serving. Nothing checks the migration itself before the views
go, because `schema plan` cannot reach an S3 root either: a refusal the
branch refusal hid shows up in the plan after the drop, in the gate while the
old pod still serves.

Outside Kubernetes (a local `docker run`) there is no API to ask, so the Lease
and the pod check are skipped. Nothing then shows that a lock's owner has
exited, so the entrypoint unlocks only when OMNIGRAPH_UNLOCK_WITHOUT_PEERS=1
says the operator knows this container is the root's only writer. Without it
a `docker run` pointed at a served root would unlock the live server.
"""

from __future__ import annotations

import contextlib
import functools
import http.client
import json
import os
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from itertools import pairwise
from pathlib import Path
from typing import Any, NoReturn

import yaml

SERVER_BINARY = "omnigraph-server"
RENDER_SCRIPT = "/usr/local/bin/render-policy-groups.py"
DEFAULT_CLUSTER_DIR = "/etc/omnigraph/cluster"
SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")

# The Lease has to outlive a missed renewal by more than the slowest child
# shutdown: ownership counts as lost RENEW_DEADLINE after the last successful
# renewal, the child then gets CHILD_STOP_SECONDS to exit, and only after that
# may the Lease expire and another holder take it.
LEASE_DURATION_SECONDS = 60
LEASE_RENEW_INTERVAL_SECONDS = 5
LEASE_RENEW_DEADLINE_SECONDS = 20
CHILD_STOP_SECONDS = 30
# A force-unlock must finish inside the Lease it was confirmed under.
UNLOCK_BUDGET_SECONDS = 15
# Reading the lock (0.13 `cluster status`, 0.14 `cluster plan`) and
# `force-unlock` read the ledger and write at most the lock. One still running
# after this is a stalled storage call, not slow progress.
LOCK_COMMAND_TIMEOUT_SECONDS = 60
# Other holders write renewTime from their own clock.
LEASE_CLOCK_SKEW_SECONDS = 5
# How much longer than the termination grace a booting pod waits for another
# server pod to exit (OMNIGRAPH_PEER_WAIT_SECONDS unset): the time the kubelet
# takes to remove the pod object. A ReplicaSet starts the replacement for a
# deleted or evicted pod while that pod is still terminating.
PEER_EXIT_MARGIN_SECONDS = 15
PEER_POLL_SECONDS = 2
KUBERNETES_TIMEOUT_SECONDS = 5

# Pod phases whose containers have all exited. An evicted pod stays behind in
# Failed until pod garbage collection, so waiting for deletion would not end.
EXITED_POD_PHASES = frozenset({"Failed", "Succeeded"})

OUTSTANDING_DEPLOYMENT = "cluster_deployment_outstanding"

# How 0.15's `cluster plan` reports a schema change refused because the graph
# has branches besides main. The diagnostic names no graph and lists only the
# first refused graph's branches, while every graph with a schema change is
# marked blocked with this reason, so the branches are read per graph.
SCHEMA_PREFLIGHT_FAILED = "schema_preflight_failed"
# The code is shared by every schema preflight refusal, and the message is the
# only thing that says which one this is.
NON_MAIN_BRANCHES_MESSAGE = "schema apply requires a graph with only main"
MAIN_BRANCH = "main"
# The prefix of every code-graph id: witan-code's `graph_id` and the
# ol-infrastructure stack's `code_graph_id` derive them byte for byte the same
# way (witan_code/config.py CODE_GRAPH_PREFIX), the bridge included.
CODE_GRAPH_PREFIX = "code-"
# One branch delete writes one graph's branch manifest; Production's took 1 to
# 3 seconds each on S3.
BRANCH_DELETE_TIMEOUT_SECONDS = 120
# The loopback server opens every graph before it serves; Production's 29
# took under a second.
LOOPBACK_START_SECONDS = 60
# The loopback server's own drain on SIGTERM (OMNIGRAPH_SHUTDOWN_GRACE_SECONDS).
# Nothing is in flight by the time it is stopped.
LOOPBACK_SHUTDOWN_GRACE_SECONDS = 5

# The server pod's terminationGracePeriodSeconds, which the kubelet's SIGKILL
# follows its SIGTERM by during the boot too (ol-infrastructure
# applications/omnigraph/data_tier.py SERVER_TERMINATION_GRACE_SECONDS).
DEFAULT_TERMINATION_GRACE_SECONDS = 125
# The part of that grace the SIGTERM cleanup leaves unspent: the in-flight
# Lease renewal (up to three Kubernetes calls of KUBERNETES_TIMEOUT_SECONDS)
# and the exit.
SIGTERM_CLEANUP_MARGIN_SECONDS = 20
# How long a child stopped after SIGTERM (the loopback server, a maintenance
# command) gets to exit before SIGKILL. It comes out of the cleanup budget. A
# loopback server killed anyway leaves a `serve` lock, which the next boot
# takes over; a maintenance command leaves its `graph_operation` lock, which
# the window removes next.
SIGTERM_STOP_SECONDS = 10

# `omnigraph version --json` reports this for 0.14's `--target` addressing.
# Upstream: clients selecting a CLI must require the exact protocol, because
# protocol 1 appeared in several incompatible edge builds.
TARGET_PROTOCOL = 2

if (
    LEASE_DURATION_SECONDS - LEASE_RENEW_DEADLINE_SECONDS
    <= CHILD_STOP_SECONDS + LEASE_CLOCK_SKEW_SECONDS
):
    msg = "the Lease can expire before a stopped child has exited"
    raise AssertionError(msg)


class BootRefused(Exception):
    """The boot cannot continue safely. The message is the operator's reason."""


# The maintenance command logs through this module under its own name.
LOG_PREFIX = "omnigraph-server"


def log(message: str) -> None:
    print(f"{LOG_PREFIX}: {message}", flush=True)


# ── Kubernetes ───────────────────────────────────────────────────────────────


class KubernetesError(Exception):
    """A Kubernetes API request failed.

    :param status: the HTTP status, or ``None`` when no response arrived.
    """

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status


class KubernetesApi:
    """The Kubernetes API calls the lock handoff and the maintenance window need.

    The ServiceAccount token is read on every request because the kubelet
    rotates the projected file in place.
    """

    def __init__(
        self, base_url: str, namespace: str, service_account_dir: Path
    ) -> None:
        self._base_url = base_url
        self._namespace = namespace
        self._token_file = service_account_dir / "token"
        self._tls = ssl.create_default_context(
            cafile=str(service_account_dir / "ca.crt")
        )

    @classmethod
    def in_cluster(cls) -> KubernetesApi:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ["KUBERNETES_SERVICE_PORT"]
        if ":" in host:
            host = f"[{host}]"
        namespace = (SERVICE_ACCOUNT_DIR / "namespace").read_text().strip()
        return cls(f"https://{host}:{port}", namespace, SERVICE_ACCOUNT_DIR)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._token_file.read_text().strip()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=KUBERNETES_TIMEOUT_SECONDS, context=self._tls
            ) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise KubernetesError(
                exc.code, f"{method} {path}: HTTP {exc.code}: {detail}"
            ) from exc
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise KubernetesError(None, f"{method} {path}: {exc}") from exc

    def _lease_path(self, name: str) -> str:
        return (
            f"/apis/coordination.k8s.io/v1/namespaces/{self._namespace}"
            f"/leases/{urllib.parse.quote(name)}"
        )

    def get_lease(self, name: str) -> dict:
        return self._request("GET", self._lease_path(name))

    def replace_lease(self, name: str, lease: dict) -> dict:
        """Replace the Lease, conditional on its ``metadata.resourceVersion``."""
        return self._request("PUT", self._lease_path(name), lease)

    def list_pods(self, label_selector: str) -> list[dict]:
        query = urllib.parse.urlencode({"labelSelector": label_selector})
        return self._request(
            "GET", f"/api/v1/namespaces/{self._namespace}/pods?{query}"
        )["items"]

    def _deployment_path(self, name: str) -> str:
        return (
            f"/apis/apps/v1/namespaces/{self._namespace}"
            f"/deployments/{urllib.parse.quote(name)}"
        )

    def get_deployment(self, name: str) -> dict:
        return self._request("GET", self._deployment_path(name))

    def replace_scale(self, name: str, replicas: int, resource_version: str) -> dict:
        """Set the Deployment's replica count, conditional on its version.

        The scale subresource carries the Deployment's own
        ``resourceVersion``, so the write fails with 409 if anything about
        the Deployment has changed since ``resource_version`` was read.
        """
        return self._request(
            "PUT",
            f"{self._deployment_path(name)}/scale",
            {
                "apiVersion": "autoscaling/v1",
                "kind": "Scale",
                "metadata": {
                    "name": name,
                    "namespace": self._namespace,
                    "resourceVersion": resource_version,
                },
                "spec": {"replicas": replicas},
            },
        )


HTTP_CONFLICT = 409


def _micro_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class UnlockLease:
    """This process's hold on the Lease every force-unlocker has to take.

    Having taken the Lease is not evidence of still holding it: during an API
    partition it can expire while this process is still writing. Ownership
    therefore counts as lost as soon as no renewal has succeeded within
    ``LEASE_RENEW_DEADLINE_SECONDS``, which is long enough before expiry for a
    running child to be stopped first (see ``run_omnigraph``).

    :param api: the Kubernetes API.
    :param name: the Lease's name. It is pre-created with no holder, because
        RBAC cannot restrict ``create`` to one name.
    :param pod_name: this pod's name. A holder left by an earlier container of
        the same pod is taken over, since the kubelet has killed that
        container before starting this one.
    """

    def __init__(self, api: KubernetesApi, name: str, pod_name: str) -> None:
        self._api = api
        self._name = name
        self._own_prefix = f"{pod_name}#"
        self.identity = f"{self._own_prefix}{secrets.token_hex(4)}"
        self._lease: dict = {}
        self._renewed_at = 0.0
        self._deposed = False
        self._mutex = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> None:
        """Take the Lease, or refuse to boot if someone else validly holds it."""
        try:
            lease = self._api.get_lease(self._name)
        except KubernetesError as exc:
            msg = f"cannot read the {self._name} Lease: {exc}"
            raise BootRefused(msg) from exc
        spec = lease.get("spec") or {}
        holder = spec.get("holderIdentity")
        if holder and not holder.startswith(self._own_prefix):
            expiry = _lease_expiry(spec)
            if expiry is None or datetime.now(UTC) < expiry:
                msg = (
                    f"the {self._name} Lease is held by {holder} "
                    f"(expires {expiry or 'unknown'}); another process is "
                    "unlocking or maintaining this cluster"
                )
                raise BootRefused(msg)
            log(f"taking over the expired {self._name} Lease from {holder}")
        now = _micro_time(datetime.now(UTC))
        lease["spec"] = {
            **spec,
            "holderIdentity": self.identity,
            "leaseDurationSeconds": LEASE_DURATION_SECONDS,
            "acquireTime": now,
            "renewTime": now,
            "leaseTransitions": int(spec.get("leaseTransitions") or 0) + 1,
        }
        started = time.monotonic()
        try:
            self._lease = self._api.replace_lease(self._name, lease)
        except KubernetesError as exc:
            msg = f"could not take the {self._name} Lease: {exc}"
            raise BootRefused(msg) from exc
        self._renewed_at = started
        self._thread = threading.Thread(
            target=self._renew_loop, name="lease-renew", daemon=True
        )
        self._thread.start()
        log(f"holding the {self._name} Lease as {self.identity}")

    def _renew_loop(self) -> None:
        while not self._stop.wait(LEASE_RENEW_INTERVAL_SECONDS):
            self._renew()

    def _renew(self) -> bool:
        """Renew once. A failure only matters once the deadline passes."""
        with self._mutex:
            if self._deposed:
                return False
            started = time.monotonic()
            try:
                self._lease = self._put_renewal(self._lease)
            except KubernetesError as exc:
                if exc.status != HTTP_CONFLICT:
                    return False
                if not self._adopt_current():
                    self._deposed = True
                    return False
                # Still the holder, on a newer resourceVersion: one more try,
                # so a metadata-only change does not fail a confirm().
                try:
                    self._lease = self._put_renewal(self._lease)
                except KubernetesError:
                    return False
            self._renewed_at = started
            return True

    def _put_renewal(self, lease: dict) -> dict:
        renewal = {
            **lease,
            "spec": {
                **lease["spec"],
                "renewTime": _micro_time(datetime.now(UTC)),
            },
        }
        return self._api.replace_lease(self._name, renewal)

    def _adopt_current(self) -> bool:
        """After a write conflict, whether the Lease still names this holder.

        Something that changes the object without touching the holder (a
        label, an annotation) moves its resourceVersion too. That is not a
        loss, so pick up the new version and let the next renewal use it.
        """
        try:
            current = self._api.get_lease(self._name)
        except KubernetesError:
            return True
        if (current.get("spec") or {}).get("holderIdentity") != self.identity:
            return False
        self._lease = current
        return True

    def lost(self) -> bool:
        return (
            self._deposed
            or time.monotonic() - self._renewed_at > LEASE_RENEW_DEADLINE_SECONDS
        )

    def confirm(self, budget_seconds: float) -> None:
        """Prove ownership now, with ``budget_seconds`` left before expiry.

        A successful conditional write to the Lease is the proof: it fails if
        anyone else has written the object since this holder last did.
        """
        if not self._renew() or self.lost():
            msg = f"lost the {self._name} Lease; not unlocking"
            raise BootRefused(msg)
        remaining = (
            self._renewed_at
            + LEASE_DURATION_SECONDS
            - LEASE_CLOCK_SKEW_SECONDS
            - time.monotonic()
        )
        if remaining <= budget_seconds:
            msg = f"the {self._name} Lease is too close to expiry; not unlocking"
            raise BootRefused(msg)

    def hand_over(self) -> None:
        """Renew one last time and stop renewing.

        The Lease is not released: it expires on its own shortly after the
        server has taken its `serve` lock, and until then nobody else may
        unlock.
        """
        self._stop_renewing()
        self._renew()

    def release(self) -> None:
        """Stop renewing and clear the holder, so the next taker need not wait.

        For a holder that has finished with the root and left it unlocked. If
        the write fails the Lease expires on its own.
        """
        self._stop_renewing()
        with self._mutex:
            if self._deposed or not self._lease:
                return
            released = {
                **self._lease,
                "spec": {**self._lease["spec"], "holderIdentity": ""},
            }
            try:
                self._api.replace_lease(self._name, released)
            except KubernetesError as exc:
                log(f"could not release the {self._name} Lease: {exc}")
            self._deposed = True

    def _stop_renewing(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()


def _lease_expiry(spec: dict) -> datetime | None:
    """When another holder's Lease stops being valid, or ``None`` if unknown."""
    renew_time = spec.get("renewTime")
    duration = spec.get("leaseDurationSeconds")
    if not renew_time or not duration:
        return None
    return datetime.fromisoformat(renew_time) + timedelta(
        seconds=int(duration) + LEASE_CLOCK_SKEW_SECONDS
    )


class NoPeers:
    """The lock handoff outside Kubernetes: no Lease and no pod list.

    :param may_unlock: whether the operator has vouched, with
        ``OMNIGRAPH_UNLOCK_WITHOUT_PEERS=1``, that nothing else writes this
        root.
    """

    def __init__(self, *, may_unlock: bool) -> None:
        self._may_unlock = may_unlock

    def lost(self) -> bool:
        return False

    def confirm(self, budget_seconds: float) -> None:
        if not self._may_unlock:
            msg = (
                "not running in Kubernetes, so nothing shows that the lock's "
                "owner has exited; set OMNIGRAPH_UNLOCK_WITHOUT_PEERS=1 if this "
                "container is the only writer of the cluster root"
            )
            raise BootRefused(msg)

    def hand_over(self) -> None:
        pass

    def release(self) -> None:
        pass


def live_pod_names(pods: list[dict], excluding: str | None = None) -> list[str]:
    """The pods whose containers may still be running, other than ``excluding``.

    Unknown (a lost node) counts as live, as it does for Kubernetes' own
    Recreate rollout.
    """
    return [
        pod["metadata"]["name"]
        for pod in pods
        if pod["metadata"]["name"] != excluding
        and (pod.get("status") or {}).get("phase") not in EXITED_POD_PHASES
    ]


def wait_for_peers_to_exit(
    api: KubernetesApi,
    selector: str,
    pod_name: str,
    lease: UnlockLease,
    wait_seconds: float,
) -> None:
    """Return once this is the only live pod behind the Deployment's selector.

    :raises BootRefused: if another pod is still live after ``wait_seconds``,
        if the selector does not match this pod, or if the Lease is lost.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        if lease.lost():
            msg = "lost the unlock Lease while waiting for other pods to exit"
            raise BootRefused(msg)
        try:
            pods = api.list_pods(selector)
        except KubernetesError as exc:
            msg = f"cannot list pods matching {selector!r}: {exc}"
            raise BootRefused(msg) from exc
        names = [pod["metadata"]["name"] for pod in pods]
        if pod_name not in names:
            # A selector that misses this pod would miss its predecessor too.
            msg = (
                f"pod selector {selector!r} does not match this pod "
                f"({pod_name}); it cannot show whether the previous server exited"
            )
            raise BootRefused(msg)
        live = live_pod_names(pods, excluding=pod_name)
        if not live:
            return
        if time.monotonic() >= deadline:
            msg = f"other server pods are still live after {wait_seconds:.0f}s: {live}"
            raise BootRefused(msg)
        log(f"waiting for other server pods to exit: {live}")
        time.sleep(PEER_POLL_SECONDS)


# ── omnigraph CLI ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str

    def json(self) -> dict[str, Any]:
        return json.loads(self.stdout)


class CommandTimedOut(Exception):
    """An omnigraph command outlived its timeout and was stopped."""


class Termination:
    """Whether SIGTERM has arrived, and what is left of the cleanup after it.

    The kubelet's SIGKILL follows its SIGTERM by the pod's termination grace,
    so from SIGTERM on, everything left to do before the exit shares one
    budget that ends inside that grace: in the boot, the in-flight branch
    delete and stopping the loopback server; in the maintenance window,
    stopping its command, removing that command's lock and the restore.

    :param cleanup_seconds: the budget, counted from SIGTERM
        (``sigterm_cleanup_seconds``).
    """

    def __init__(self, cleanup_seconds: float) -> None:
        self.cleanup_seconds = cleanup_seconds
        self._deadline: float | None = None

    def note(self) -> None:
        """Record SIGTERM. A second one does not move the deadline."""
        if self._deadline is None:
            self._deadline = time.monotonic() + self.cleanup_seconds

    def is_set(self) -> bool:
        return self._deadline is not None

    def remaining(self) -> float:
        """Seconds left of the budget, unbounded before SIGTERM."""
        if self._deadline is None:
            return float("inf")
        return max(0.0, self._deadline - time.monotonic())

    def stop_seconds(self) -> float:
        """How long a child being stopped now gets before SIGKILL."""
        if self._deadline is None:
            return CHILD_STOP_SECONDS
        return min(SIGTERM_STOP_SECONDS, self.remaining())


def run_omnigraph(
    args: list[str],
    lease: UnlockLease | NoPeers,
    timeout_seconds: float | None = None,
    interrupt: Callable[[], None] | None = None,
    terminated: Termination | None = None,
) -> CommandResult:
    """Run one omnigraph CLI command, stopping it if the Lease is lost.

    stderr goes straight to the pod log: it carries upstream's own
    diagnostics, lock ids and deployment ids.

    The command never outlives this call. Whatever ends the wait (the Lease,
    the timeout, ``interrupt``), the child is stopped first, so its I/O is
    over before anything acts on the lock it held.

    :param interrupt: called about once a second while the command runs. It
        raises to stop the command, and that exception propagates.
    :param terminated: once SIGTERM is recorded there, the command runs only
        while its cleanup budget lasts, and a stop gets what is left of it.
    :raises BootRefused: if the Lease was lost while the command ran. Nothing
        is unlocked.
    :raises CommandTimedOut: if the command ran longer than ``timeout_seconds``
        or past the end of the cleanup budget.
    """
    command = f"`omnigraph {' '.join(args)}`"
    if terminated is not None and terminated.remaining() <= 0:
        # A command that would finish inside the first poll still runs in the
        # margin left for the exit.
        msg = f"{command} not started: the cleanup budget after SIGTERM is spent"
        raise CommandTimedOut(msg)
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    with subprocess.Popen(
        ["omnigraph", *args],
        stdout=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            while True:
                try:
                    stdout, _ = child.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    if lease.lost():
                        msg = f"lost the unlock Lease during {command}"
                        raise BootRefused(msg) from None
                    if deadline is not None and time.monotonic() >= deadline:
                        msg = f"{command} did not finish in {timeout_seconds:.0f}s"
                        raise CommandTimedOut(msg) from None
                    if terminated is not None and terminated.remaining() <= 0:
                        msg = f"{command} outlasted the cleanup budget after SIGTERM"
                        raise CommandTimedOut(msg) from None
                    if interrupt is not None:
                        interrupt()
                else:
                    return CommandResult(child.returncode, stdout)
        except BaseException:
            stop_child(
                child,
                CHILD_STOP_SECONDS if terminated is None else terminated.stop_seconds(),
            )
            raise


def stop_child(child: subprocess.Popen, stop_seconds: float) -> None:
    """SIGTERM ``child``, then SIGKILL it if it outlives ``stop_seconds``."""
    child.terminate()
    try:
        child.wait(timeout=stop_seconds)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


@functools.cache
def cli_speaks_targets() -> bool:
    """Whether the `omnigraph` CLI is addressed with 0.14's ``--target``.

    0.13's `version` takes no ``--json`` and exits 2 on it.

    :raises BootRefused: on a CLI that speaks another target protocol.
    """
    result = subprocess.run(
        ["omnigraph", "version", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    protocol = json.loads(result.stdout)["protocols"]["target"]
    if protocol != TARGET_PROTOCOL:
        msg = (
            f"the omnigraph CLI speaks target protocol {protocol}; this image "
            f"supports {TARGET_PROTOCOL} (omnigraph 0.14) or a pre-0.14 CLI"
        )
        raise BootRefused(msg)
    return True


def cluster_layout(config_dir: Path) -> tuple[str, list[str]]:
    """The storage root and graph ids `cluster.yaml` declares.

    Without `storage`, the config directory is the root.
    """
    declared = yaml.safe_load((config_dir / "cluster.yaml").read_text())
    return declared.get("storage") or str(config_dir.absolute()), list(
        declared["graphs"]
    )


def inline_target(config_dir: Path, root: str) -> str:
    """`root` as a 0.14 inline `--target`.

    0.14 resolves a relative local `storage` against the config directory
    (`bind_direct_source_root`), while a relative `--target` is read as a
    registered target name or against the working directory, so the two would
    disagree and the plan refuses the mismatch.
    """
    if "://" in root or Path(root).is_absolute():
        return root
    return str((config_dir / root).absolute())


# Diagnostics `cluster plan` attaches when the lock exists but cannot be read.
# The lock then reads as unlocked, or as locked without an id.
LOCK_READ_DIAGNOSTICS = frozenset(
    {"state_lock_read_error", "invalid_state_lock", "unsupported_state_lock_version"}
)


class Cluster:
    """The omnigraph commands against one config directory and its root.

    The CLI's own version picks the argument shapes (``cli_speaks_targets``).

    :param terminated: bounds the lock commands by the cleanup budget once
        SIGTERM is recorded there (``run_omnigraph``).
    :raises BootRefused: on a CLI whose dialect this does not know.
    """

    def __init__(
        self,
        config_dir: Path,
        actor: str | None,
        lease: UnlockLease | NoPeers,
        terminated: Termination | None = None,
    ) -> None:
        self._config_dir = str(config_dir)
        self._as = ["--as", actor] if actor else []
        self._lease = lease
        self._terminated = terminated
        self._targets = cli_speaks_targets()
        if self._targets:
            root, _ = cluster_layout(config_dir)
            self._target = ["--target", inline_target(config_dir, root)]
            self._source = ["--config", self._config_dir, *self._target]
        else:
            self._source = ["--config", self._config_dir]

    def _run(self, *args: str) -> CommandResult:
        return run_omnigraph(["cluster", *args], self._lease)

    def _run_lock_command(self, *args: str) -> CommandResult:
        try:
            return run_omnigraph(
                ["cluster", *args],
                self._lease,
                timeout_seconds=LOCK_COMMAND_TIMEOUT_SECONDS,
                terminated=self._terminated,
            )
        except CommandTimedOut as exc:
            raise BootRefused(str(exc)) from exc

    def lock(self) -> dict[str, Any]:
        """The lock's ``state_observations``.

        0.13 reads them from `cluster status`. 0.14's `status` reports no
        lock operation and `cluster observe` takes no inline `s3://` root, so
        they come from `cluster plan`, which reads the root without taking
        the lock.

        :raises BootRefused: if the lock's state cannot be read.
        """
        if not self._targets:
            result = self._run_lock_command("status", *self._source, "--json")
            if result.returncode != 0:
                msg = f"`cluster status` failed (exit {result.returncode})"
                raise BootRefused(msg)
            try:
                return result.json()["state_observations"]
            except (ValueError, KeyError) as exc:
                msg = f"`cluster status` printed no lock state: {result.stdout[:200]!r}"
                raise BootRefused(msg) from exc
        result = self._run_lock_command("plan", *self._source, "--json")
        try:
            plan = result.json()
            observations = plan["state_observations"]
        except (ValueError, KeyError) as exc:
            msg = f"`cluster plan` printed no lock state: {result.stdout[:200]!r}"
            raise BootRefused(msg) from exc
        # A plan refused before it reaches the root (an invalid config, a
        # storage mismatch) still prints `locked: false`. The lock is read
        # before the ledger, so a ledger found means the lock was read too;
        # without one only an error-free plan has reached the root. Errors
        # after that (a schema change planned without an actor) say nothing
        # about the lock.
        diagnostics = plan["diagnostics"]
        unread = [d for d in diagnostics if d["code"] in LOCK_READ_DIAGNOSTICS]
        if not observations["state_found"]:
            unread += [d for d in diagnostics if d["severity"] == "error"]
        if unread:
            msg = f"`cluster plan` could not read the lock: {json.dumps(unread)}"
            raise BootRefused(msg)
        return observations

    def plan(self) -> dict[str, Any]:
        """The plan, which reads the root without taking the writer lock.

        A plan that finds problems exits non-zero and still prints its JSON.
        """
        result = self._run("plan", *self._source, *self._as, "--json")
        try:
            plan = result.json()
        except ValueError as exc:
            msg = f"`cluster plan` printed no plan (exit {result.returncode})"
            raise BootRefused(msg) from exc
        if "state_observations" not in plan:
            # 0.14 refuses a target before planning with an `error` object.
            msg = f"`cluster plan` printed no plan: {result.stdout[:500]!r}"
            raise BootRefused(msg)
        return plan

    def apply(self) -> None:
        result = self._run("apply", *self._source, *self._as, "--json")
        if result.returncode != 0:
            sys.stdout.write(result.stdout)
            msg = f"`cluster apply` failed (exit {result.returncode})"
            raise BootRefused(msg)

    def force_unlock(self, lock_id: str) -> None:
        self._lease.confirm(UNLOCK_BUDGET_SECONDS)
        # 0.14's force-unlock takes no `--config` and needs an explicit target.
        scope = self._target if self._targets else self._source
        result = self._run_lock_command("force-unlock", *scope, lock_id)
        if result.returncode != 0:
            msg = f"`cluster force-unlock {lock_id}` failed (exit {result.returncode})"
            raise BootRefused(msg)
        sys.stdout.write(result.stdout)

    def graph_command(self, command: str, graph: str, *args: str) -> list[str]:
        """The argv of a direct maintenance command on one graph of the root."""
        if self._targets:
            scope = self._target
        else:
            scope = ["--cluster", cluster_layout(Path(self._config_dir))[0]]
        return [command, *scope, "--graph", graph, *args]


# ── Branch views over HTTP ───────────────────────────────────────────────────


class ServerUnavailable(BootRefused):
    """The server answering for the branch views could not be reached."""


@functools.cache
def http_contract() -> str:
    """The HTTP API contract this image's omnigraph speaks.

    The server refuses a request without exactly this `Omnigraph-Http-Api`
    header value, and `version --json` reports it from 0.14 on.
    """
    result = subprocess.run(
        ["omnigraph", "version", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)["http_contract"]


def view_drop_token(env: dict[str, str]) -> str:
    """OMNIGRAPH_VIEW_DROP_ACTOR's bearer token, from the server's token map."""
    tokens_file = Path(env["OMNIGRAPH_SERVER_BEARER_TOKENS_FILE"])
    return json.loads(tokens_file.read_text())[env["OMNIGRAPH_VIEW_DROP_ACTOR"]]


class ServedGraphs:
    """A server's branch API: list and delete one graph's branches.

    :param base_url: the server's root URL, e.g. ``http://127.0.0.1:8080``.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Omnigraph-Http-Api": http_contract(),
        }

    def _call(
        self, method: str, path: str, timeout: float, *, missing_ok: bool = False
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(url, method=method, headers=self._headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if missing_ok and exc.code == HTTPStatus.NOT_FOUND:
                return {}
            msg = f"{method} {url}: HTTP {exc.code}: {exc.read()[:300]!r}"
            if exc.code >= HTTPStatus.INTERNAL_SERVER_ERROR:
                raise ServerUnavailable(msg) from exc
            raise BootRefused(msg) from exc
        except OSError as exc:
            msg = f"{method} {url}: {exc}"
            raise ServerUnavailable(msg) from exc

    def serving(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/healthz", timeout=1) as resp:
                return resp.status == HTTPStatus.OK
        except OSError:
            return False

    def branches(self, graph: str) -> list[str]:
        """``graph``'s branches other than main."""
        path = f"/graphs/{urllib.parse.quote(graph, safe='')}/branches"
        listed = self._call("GET", path, LOCK_COMMAND_TIMEOUT_SECONDS)["branches"]
        return sorted(branch for branch in listed if branch != MAIN_BRANCH)

    def delete(self, graph: str, branch: str, timeout: float) -> None:
        """Delete one branch. One already gone (the reaper's) counts as deleted."""
        path = (
            f"/graphs/{urllib.parse.quote(graph, safe='')}"
            f"/branches/{urllib.parse.quote(branch, safe='')}"
        )
        self._call("DELETE", path, timeout, missing_ok=True)


@contextlib.contextmanager
def loopback_server(
    config_dir: Path,
    env: dict[str, str],
    lease: UnlockLease | NoPeers,
    terminated: Termination,
) -> Iterator[ServedGraphs]:
    """Serve the root on loopback for its branch API, and stop it on exit.

    An S3 root is served config-free by its URI, which is how the drop was
    first run in Production. A local root is served through the config
    directory, which serves the applied state whatever schema change
    `cluster.yaml` has pending. The server takes the `serve` lock, and from
    0.14 on releases it on a clean exit.

    :raises BootRefused: if the server exits or does not serve within
        LOOPBACK_START_SECONDS, or the Lease is lost or SIGTERM arrives while
        it starts.
    """
    root = cluster_layout(config_dir)[0]
    source = root if "://" in root else str(config_dir.absolute())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    served = ServedGraphs(f"http://127.0.0.1:{port}", view_drop_token(env))
    log(f"serving {source} on {served.base_url} to drop branch views")
    child = subprocess.Popen(
        [SERVER_BINARY, "--cluster", source, "--bind", f"127.0.0.1:{port}"],
        env={
            **os.environ,
            **env,
            "OMNIGRAPH_SHUTDOWN_GRACE_SECONDS": str(LOOPBACK_SHUTDOWN_GRACE_SECONDS),
        },
    )
    try:
        deadline = time.monotonic() + LOOPBACK_START_SECONDS
        while not served.serving():
            if child.poll() is not None:
                msg = (
                    f"the loopback omnigraph-server exited ({child.returncode}) "
                    "before serving"
                )
                raise BootRefused(msg)
            if lease.lost():
                msg = "lost the unlock Lease while the loopback server started"
                raise BootRefused(msg)
            if terminated.is_set():
                msg = "SIGTERM while the loopback server started"
                raise BootRefused(msg)
            if time.monotonic() >= deadline:
                msg = (
                    "the loopback omnigraph-server did not serve within "
                    f"{LOOPBACK_START_SECONDS}s"
                )
                raise BootRefused(msg)
            time.sleep(0.2)
        yield served
    finally:
        stop_child(child, terminated.stop_seconds())


# ── Boot sequence ────────────────────────────────────────────────────────────
# ── Boot sequence ────────────────────────────────────────────────────────────


def render_policy_groups(config_dir: Path, tokens_file: str | None) -> bool:
    """Rewrite the bundles' group membership from the live actor-token map.

    The bundles ship with fixture ids, so applying them unrendered would
    authenticate the real service accounts and then deny them everything.
    Done on the restart path because `witan-users` has to track the hourly
    token-sync job's output, which Pulumi cannot see. See
    policy/render_groups.py.

    :returns: whether the image carries policy bundles. None is the
        pre-policy default-deny configuration and has to keep booting.
    :raises BootRefused: on bundles without a token map. omnigraph-server
        would refuse to boot on that anyway, three layers further down.
    """
    bundles = sorted(str(path) for path in config_dir.glob("*.policy.yaml"))
    if not bundles:
        return False
    if not tokens_file or not Path(tokens_file).is_file():
        msg = (
            "policy bundles present but no bearer-token map "
            "(OMNIGRAPH_SERVER_BEARER_TOKENS_FILE unset or missing)"
        )
        raise BootRefused(msg)
    log(f"rendering policy group membership from {tokens_file}")
    subprocess.run(
        [sys.executable, RENDER_SCRIPT, "--tokens", tokens_file, *bundles],
        check=True,
    )
    return True


def check_plan(
    plan: dict[str, Any],
    removable_graphs: frozenset[str],
    *,
    views_pending: bool = False,
) -> None:
    """Refuse a plan the apply must not act on.

    :param removable_graphs: graph ids the deployment has explicitly allowed
        to be deleted. From 0.13 on, applying a `cluster.yaml` that no longer
        declares a graph deletes its storage and history, and the generated
        `cluster.yaml` reaches this pod through a ConfigMap that a failed
        deploy does not roll back.
    :param views_pending: the plan was refused only because of branch views
        the boot will drop (``views_to_drop``), so its refusal is not one.
    :raises BootRefused: on an outstanding deployment, a graph removal that
        is not allow-listed, or any other plan error.
    """
    diagnostics = plan.get("diagnostics") or []
    if any(d.get("code") == OUTSTANDING_DEPLOYMENT for d in diagnostics):
        msg = (
            "an earlier `cluster apply` was interrupted and its deployment is "
            "still outstanding; reconcile it by its exact id (runbook) before "
            "this pod can unlock or serve"
        )
        raise BootRefused(msg)
    removed = sorted(
        change["resource"].removeprefix("graph.")
        for change in plan.get("changes") or []
        if change["resource"].startswith("graph.") and change["operation"] == "delete"
    )
    refused = [graph for graph in removed if graph not in removable_graphs]
    if refused:
        msg = (
            f"cluster.yaml no longer declares {refused}, and applying it would "
            "delete their storage and history; restore them or add them to "
            "OMNIGRAPH_GRAPH_REMOVAL_ALLOWLIST"
        )
        raise BootRefused(msg)
    if removed:
        log(f"this apply deletes allow-listed graphs: {removed}")
    if not plan.get("ok") and not views_pending:
        msg = f"`cluster plan` refused the configuration: {json.dumps(diagnostics)}"
        raise BootRefused(msg)


def graph_list(env: dict[str, str], name: str) -> frozenset[str]:
    """The comma-separated graph ids in the environment variable ``name``."""
    return frozenset(
        graph.strip() for graph in env.get(name, "").split(",") if graph.strip()
    )


def branch_drop_allowlist(
    env: dict[str, str], declared_graphs: list[str]
) -> frozenset[str]:
    """The graphs whose non-main branches a boot may delete.

    :raises BootRefused: on an id that is not a code graph's, so that a
        mistaken list cannot reach the council graph, or one `cluster.yaml`
        does not declare. A typo would otherwise allow nothing, and the
        schema change it was meant to let through would stall the deploy
        again.
    """
    allowed = graph_list(env, "OMNIGRAPH_BRANCH_DROP_ALLOWLIST")
    not_code = sorted(
        graph for graph in allowed if not graph.startswith(CODE_GRAPH_PREFIX)
    )
    if not_code:
        msg = (
            f"OMNIGRAPH_BRANCH_DROP_ALLOWLIST names {not_code}, which are not "
            f"code graphs ({CODE_GRAPH_PREFIX}* ids); only a code graph's "
            "branches are derived views"
        )
        raise BootRefused(msg)
    unknown = sorted(allowed - set(declared_graphs))
    if unknown:
        msg = (
            f"OMNIGRAPH_BRANCH_DROP_ALLOWLIST names {unknown}, which "
            "cluster.yaml does not declare"
        )
        raise BootRefused(msg)
    return allowed


def branch_blocked_graphs(plan: dict[str, Any]) -> list[str]:
    """The graphs whose schema change the plan refused for non-main branches.

    Empty unless that is the plan's only error. The plan marks every graph
    with a schema change blocked, including ones with no branches, so the
    branches themselves are read afterwards (``views_to_drop``).
    """
    errors = [d for d in plan.get("diagnostics") or [] if d.get("severity") == "error"]
    if not errors or not all(
        d.get("code") == SCHEMA_PREFLIGHT_FAILED
        and d.get("message", "").startswith(NON_MAIN_BRANCHES_MESSAGE)
        for d in errors
    ):
        return []
    return sorted(
        change["resource"].removeprefix("schema.")
        for change in plan.get("changes") or []
        if change["resource"].startswith("schema.")
        and change.get("reason") == SCHEMA_PREFLIGHT_FAILED
    )


def views_to_drop(
    served: ServedGraphs,
    blocked: list[str],
    droppable_graphs: frozenset[str],
) -> dict[str, list[str]]:
    """The branches to delete so the plan's schema changes can apply.

    Only allow-listed graphs are listed: the view-drop actor may read nothing
    else (the reaper's grants), and only their branches are views. The plan
    marks every graph with a schema change blocked, so when none of these has
    a branch the refusal is another graph's.

    :param blocked: the graphs whose schema change the plan refused for
        branches (``blocked_graphs``).
    :param droppable_graphs: graphs whose non-main branches are derived
        views (``OMNIGRAPH_BRANCH_DROP_ALLOWLIST``).
    :returns: each blocked, allow-listed graph's non-main branches.
    :raises BootRefused: when none of them has a branch. A graph that is not
        allow-listed has them, and they may hold data nothing re-derives, so
        they are left for a person to resolve.
    """
    branched = {
        graph: branches
        for graph in blocked
        if graph in droppable_graphs and (branches := served.branches(graph))
    }
    if not branched:
        kept = sorted(set(blocked) - droppable_graphs)
        msg = (
            "omnigraph applies a schema change only to a graph with no branch "
            f"but main, and of {kept}, which are not in "
            "OMNIGRAPH_BRANCH_DROP_ALLOWLIST, one has others. They may hold data "
            "nothing re-derives; merge or delete them first (runbook)"
        )
        raise BootRefused(msg)
    return branched


def termination_grace_seconds(env: dict[str, str]) -> float:
    """The server pod's terminationGracePeriodSeconds, as its stack passes it."""
    return float(
        env.get(
            "OMNIGRAPH_TERMINATION_GRACE_SECONDS", DEFAULT_TERMINATION_GRACE_SECONDS
        )
    )


def peer_wait_seconds(env: dict[str, str]) -> float:
    """How long a booting pod waits for another server pod to exit.

    Another server pod gets the same grace as this one to exit.
    """
    if "OMNIGRAPH_PEER_WAIT_SECONDS" in env:
        return float(env["OMNIGRAPH_PEER_WAIT_SECONDS"])
    return termination_grace_seconds(env) + PEER_EXIT_MARGIN_SECONDS


def sigterm_cleanup_seconds(env: dict[str, str]) -> float:
    """The cleanup budget after SIGTERM: the pod's grace less a margin.

    :raises BootRefused: if the grace leaves no time to stop the loopback
        server.
    """
    grace = termination_grace_seconds(env)
    budget = grace - SIGTERM_CLEANUP_MARGIN_SECONDS
    if budget <= SIGTERM_STOP_SECONDS:
        msg = (
            f"OMNIGRAPH_TERMINATION_GRACE_SECONDS={grace:.0f} leaves no time to "
            "stop the loopback server after SIGTERM; it has to exceed "
            f"{SIGTERM_CLEANUP_MARGIN_SECONDS + SIGTERM_STOP_SECONDS}"
        )
        raise BootRefused(msg)
    return budget


@contextlib.contextmanager
def sigterm_noted(
    cleanup_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS
    - SIGTERM_CLEANUP_MARGIN_SECONDS,
) -> Iterator[Termination]:
    """Record SIGTERM for as long as the boot runs.

    As PID 1 with no handler the entrypoint ignores SIGTERM, and the kill
    comes as SIGKILL at the end of the grace period. Only the branch-view
    drop acts on it (``drop_branch_views``, ``converge``). It is installed
    before the Lease is taken, so a SIGTERM that arrives while the boot is
    still planning stops it before its first delete.

    :param cleanup_seconds: the budget SIGTERM starts
        (``sigterm_cleanup_seconds``).
    """
    terminated = Termination(cleanup_seconds)
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: terminated.note())
    try:
        yield terminated
    finally:
        signal.signal(signal.SIGTERM, previous)


def refuse_on_sigterm(terminated: Termination, when: str) -> None:
    """End the boot before the apply once SIGTERM has arrived.

    The kubelet's SIGKILL could cut the apply off and leave an outstanding
    deployment, which every later boot refuses.
    """
    if terminated.is_set():
        msg = f"SIGTERM {when}; not applying, the next boot does"
        raise BootRefused(msg)


def drop_branch_views(
    served: ServedGraphs,
    views: dict[str, list[str]],
    terminated: Termination,
    lease: UnlockLease | NoPeers,
) -> None:
    """Delete every branch in ``views`` through ``served``, logging what went.

    A failure stops with the deletes so far kept. The views are caches, so a
    graph left with some of them loses nothing, and the next gate or boot
    plans again and deletes the rest. SIGTERM (the startupProbe giving up on
    a long drop, a rollout, a drain) or a lost Lease stops it the same way
    once the in-flight delete returns.

    :param terminated: records SIGTERM (``sigterm_noted``).
    :raises BootRefused: on the first delete that fails, on SIGTERM, or once
        the Lease is lost.
    """
    for graph, branches in views.items():
        log(
            f"dropping {len(branches)} branch views on {graph} so its "
            f"schema change can apply: {', '.join(branches)}"
        )
        for dropped, branch in enumerate(branches):
            try:
                if terminated.is_set():
                    msg = "SIGTERM while dropping branch views"
                    raise BootRefused(msg)
                if lease.lost():
                    msg = "lost the unlock Lease while dropping branch views"
                    raise BootRefused(msg)
                timeout = min(BRANCH_DELETE_TIMEOUT_SECONDS, terminated.remaining())
                served.delete(graph, branch, timeout)
            except BootRefused:
                log(
                    f"dropped {dropped} of {len(branches)} branch views on "
                    f"{graph}; the rest are dropped on the next attempt"
                )
                raise
        log(f"dropped {len(branches)} branch views on {graph}")


def release_previous_lock(cluster: Cluster) -> None:
    """Remove the lock the previous owner of this root left behind.

    Only called once the Lease is held, no other server pod is live and the
    plan reports no outstanding deployment, so a `serve` or `deployment` lock
    belongs to a process that has exited. A `graph_operation` lock is a
    maintenance run's, which is not in the pod list, so it is never taken
    over here.

    On 0.14 a server that shut down cleanly leaves no lock, so this unlocks
    only after a crash, a kill or uncertain storage work. It still has to:
    the direct apply that follows refuses a held lock, leased or not, where
    only the server's own `serve` acquisition would take one over.
    """
    lock = cluster.lock()
    if not lock["locked"]:
        return
    operation = lock["lock_operation"]
    if operation not in ("serve", "deployment"):
        msg = (
            f"the cluster is locked by a {operation!r} operation "
            f"(lock id {lock['lock_id']}); this entrypoint does not take that "
            "over (runbook)"
        )
        raise BootRefused(msg)
    log(f"releasing the {operation} lock left by the previous owner")
    cluster.force_unlock(lock["lock_id"])


def release_apply_lock(cluster: Cluster) -> None:
    """Remove the `deployment` lock this boot's own apply just left.

    0.14's apply releases it itself only on a refusal before any effect
    started; a converged no-op apply still leaves it.
    """
    lock = cluster.lock()
    if not lock["locked"]:
        return
    if lock["lock_operation"] != "deployment":
        msg = (
            f"after apply the cluster is locked by a {lock['lock_operation']!r} "
            f"operation (lock id {lock['lock_id']}), which this boot did not take"
        )
        raise BootRefused(msg)
    cluster.force_unlock(lock["lock_id"])


def checked_plan(
    config_dir: Path, env: dict[str, str], lease: UnlockLease | NoPeers
) -> tuple[Cluster, list[str]]:
    """Plan the baked configuration against the root, refusing a bad plan.

    Reads the root and changes nothing in it, so the pre-deploy gate
    (omnigraph_plan_gate.py) runs exactly this beside a serving pod.

    :returns: the cluster the plan was made for, ready to apply, and the
        graphs whose branch views have to go first (``blocked_graphs``).
    :raises BootRefused: see ``check_plan`` and ``render_policy_groups``.
    """
    has_policy = render_policy_groups(
        config_dir, env.get("OMNIGRAPH_SERVER_BEARER_TOKENS_FILE")
    )
    actor = env.get("OMNIGRAPH_APPLY_ACTOR")
    if has_policy and not actor:
        # A no-op apply passes without an actor and the first schema change
        # does not, which would turn a deploy into an outage weeks later.
        msg = (
            "policy bundles present but OMNIGRAPH_APPLY_ACTOR is unset; with "
            "policy installed, omnigraph refuses schema changes that name no actor"
        )
        raise BootRefused(msg)
    cluster = Cluster(config_dir, actor, lease)
    return cluster, blocked_graphs(cluster, config_dir, env)


def blocked_graphs(
    cluster: Cluster, config_dir: Path, env: dict[str, str]
) -> list[str]:
    """Plan, refuse a bad plan, and return the graphs whose branches block it.

    A plan refused only for branches passes when an allow-listed graph is
    among those blocked; ``views_to_drop`` then refuses any blocked graph that
    is not allow-listed and has branches. Without one, the refusal stands.
    """
    removable = graph_list(env, "OMNIGRAPH_GRAPH_REMOVAL_ALLOWLIST")
    droppable = branch_drop_allowlist(env, cluster_layout(config_dir)[1])
    plan = cluster.plan()
    blocked = branch_blocked_graphs(plan)
    check_plan(plan, removable, views_pending=not droppable.isdisjoint(blocked))
    return blocked


def drop_views_on_loopback(
    config_dir: Path,
    env: dict[str, str],
    lease: UnlockLease | NoPeers,
    terminated: Termination,
    blocked: list[str],
) -> None:
    """Delete the views ``blocked`` graphs still have, with nothing serving."""
    droppable = branch_drop_allowlist(env, cluster_layout(config_dir)[1])
    with loopback_server(config_dir, env, lease, terminated) as served:
        views = views_to_drop(served, blocked, droppable)
        drop_branch_views(served, views, terminated, lease)


def converge(
    config_dir: Path,
    env: dict[str, str],
    lease: UnlockLease | NoPeers,
    terminated: Termination | None = None,
) -> None:
    """Bring the cluster root to the baked configuration, unlocked.

    :param terminated: records SIGTERM (``sigterm_noted``).
    """
    if terminated is None:
        terminated = Termination(sigterm_cleanup_seconds(env))
    cluster, blocked = checked_plan(config_dir, env, lease)
    release_previous_lock(cluster)
    if blocked:
        drop_views_on_loopback(config_dir, env, lease, terminated, blocked)
        # A last delete that returned before SIGTERM was checked ends the drop
        # without raising.
        refuse_on_sigterm(terminated, "after dropping the branch views")
        # A loopback server that had to be killed leaves its `serve` lock.
        release_previous_lock(cluster)
        # The first plan saw only the branch refusal, which can hide another
        # one behind it, so the apply waits for a plan with no refusal at all.
        remaining = blocked_graphs(cluster, config_dir, env)
        if remaining:
            msg = (
                f"branches still block the schema change of {remaining} after "
                "the drop, so a graph not in OMNIGRAPH_BRANCH_DROP_ALLOWLIST has "
                "them; merge or delete them first (runbook)"
            )
            raise BootRefused(msg)
        refuse_on_sigterm(terminated, "while planning after the drop")
    log(f"converging cluster catalog at {config_dir}")
    cluster.apply()
    release_apply_lock(cluster)


def take_over(env: dict[str, str]) -> UnlockLease | NoPeers:
    """Establish that this process may unlock: the Lease, then the pod check."""
    if "KUBERNETES_SERVICE_HOST" not in env:
        log("not running in Kubernetes; skipping the unlock Lease and pod check")
        return NoPeers(may_unlock=env.get("OMNIGRAPH_UNLOCK_WITHOUT_PEERS") == "1")
    api = KubernetesApi.in_cluster()
    pod_name = env.get("POD_NAME") or env["HOSTNAME"]
    lease = UnlockLease(api, env["OMNIGRAPH_UNLOCK_LEASE"], pod_name)
    lease.acquire()
    wait_for_peers_to_exit(
        api,
        env["OMNIGRAPH_POD_SELECTOR"],
        pod_name,
        lease,
        peer_wait_seconds(env),
    )
    return lease


def cluster_argument(argv: list[str], env: dict[str, str]) -> str:
    """The `--cluster` value forwarded to omnigraph-server.

    Read from the server's own arguments so this stays in lockstep with the
    Deployment's `args`.
    """
    for argument in argv:
        if argument.startswith("--cluster="):
            return argument.removeprefix("--cluster=")
    for flag, value in pairwise(argv):
        if flag == "--cluster":
            return value
    return env.get("OMNIGRAPH_CLUSTER_DIR", DEFAULT_CLUSTER_DIR)


def main(argv: list[str]) -> NoReturn:
    env = dict(os.environ)
    cluster_arg = cluster_argument(argv, env)
    if cluster_arg.startswith(("s3://", "http://", "https://")):
        # Serving straight from a storage-root URI: there is no local
        # cluster.yaml to converge. On 0.13 nothing releases a lock a
        # previous server left on that root; 0.14 on S3 takes over a crashed
        # server's lock once its lease has been silent for 45s.
        log(f"--cluster is a storage URI ({cluster_arg}); skipping cluster bootstrap")
    else:
        try:
            with sigterm_noted(sigterm_cleanup_seconds(env)) as terminated:
                lease = take_over(env)
                converge(Path(cluster_arg), env, lease, terminated)
                lease.hand_over()
        except BootRefused as refusal:
            print(f"omnigraph-server: refusing to start: {refusal}", file=sys.stderr)
            sys.exit(1)
        log("cluster converged; starting server")
    sys.stdout.flush()
    os.execvp(SERVER_BINARY, [SERVER_BINARY, *argv])


if __name__ == "__main__":
    main(sys.argv[1:])
