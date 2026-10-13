#!/usr/bin/env python3
"""Entrypoint for the omnigraph-server data-tier image (omnigraph 0.13).

omnigraph-server serves the graphs that `omnigraph cluster apply` has created
under the storage root and never creates them itself, so this converges the
cluster in the container's own start path before exec'ing the server. A direct
apply on an empty root creates the ledger and the graphs; on a converged root
it is a no-op.

WHY THIS IS NOT A SHELL SCRIPT ANY MORE

From 0.12 on, every writer of a cluster root persists an exclusive lock at
`<root>/__cluster/lock.json` and keeps it after it exits: the server leaves
`serve` (also after a clean SIGTERM, and after a start that fails), a direct
`cluster apply` leaves `deployment`, `optimize` and `cleanup` leave
`graph_operation`. The next writer is refused with `state_lock_held` until
someone runs `cluster force-unlock <id>`. This pod restarts on its own (hourly
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
               is not allow-listed, or on any other plan error
    status  -> serve / deployment lock: force-unlock; anything else: exit 1
    apply
    status  -> force-unlock the deployment lock apply left
    exec omnigraph-server

Design and the binary-verified facts behind each step:
docs/internals/design/omnigraph-0-13-upgrade-spec.md (D3, D4).

Outside Kubernetes (a local `docker run`) there is no API to ask, so the Lease
and the pod check are skipped. Nothing then shows that a lock's owner has
exited, so the entrypoint unlocks only when OMNIGRAPH_UNLOCK_WITHOUT_PEERS=1
says the operator knows this container is the root's only writer. Without it
a `docker run` pointed at a served root would unlock the live server.
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, NoReturn

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
# `cluster status` and `force-unlock` read and write two small objects. One
# still running after this is a stalled storage call, not slow progress.
LOCK_COMMAND_TIMEOUT_SECONDS = 60
# Other holders write renewTime from their own clock.
LEASE_CLOCK_SKEW_SECONDS = 5
# terminationGracePeriodSeconds (35) plus a margin for the kubelet to remove
# the pod object.
DEFAULT_PEER_WAIT_SECONDS = 50
PEER_POLL_SECONDS = 2
KUBERNETES_TIMEOUT_SECONDS = 5

# Pod phases whose containers have all exited. An evicted pod stays behind in
# Failed until pod garbage collection, so waiting for deletion would not end.
EXITED_POD_PHASES = frozenset({"Failed", "Succeeded"})

OUTSTANDING_DEPLOYMENT = "cluster_deployment_outstanding"

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


def run_omnigraph(
    args: list[str],
    lease: UnlockLease | NoPeers,
    timeout_seconds: float | None = None,
    interrupt: Callable[[], None] | None = None,
) -> CommandResult:
    """Run one omnigraph CLI command, stopping it if the Lease is lost.

    stderr goes straight to the pod log: it carries upstream's own
    diagnostics, lock ids and deployment ids.

    The command never outlives this call. Whatever ends the wait (the Lease,
    the timeout, ``interrupt``), the child is stopped first, so its I/O is
    over before anything acts on the lock it held.

    :param interrupt: called about once a second while the command runs. It
        raises to stop the command, and that exception propagates.
    :raises BootRefused: if the Lease was lost while the command ran. Nothing
        is unlocked.
    :raises CommandTimedOut: if the command ran longer than ``timeout_seconds``.
    """
    command = f"`omnigraph {' '.join(args)}`"
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
                    if interrupt is not None:
                        interrupt()
                else:
                    return CommandResult(child.returncode, stdout)
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=CHILD_STOP_SECONDS)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise


class Cluster:
    """The `omnigraph cluster` commands against one config directory."""

    def __init__(
        self, config_dir: Path, actor: str | None, lease: UnlockLease | NoPeers
    ) -> None:
        self._config_dir = str(config_dir)
        self._as = ["--as", actor] if actor else []
        self._lease = lease

    def _run(self, *args: str) -> CommandResult:
        return run_omnigraph(["cluster", *args], self._lease)

    def _run_lock_command(self, *args: str) -> CommandResult:
        try:
            return run_omnigraph(
                ["cluster", *args],
                self._lease,
                timeout_seconds=LOCK_COMMAND_TIMEOUT_SECONDS,
            )
        except CommandTimedOut as exc:
            raise BootRefused(str(exc)) from exc

    def lock(self) -> dict[str, Any]:
        """The ``state_observations`` of `cluster status`.

        :raises BootRefused: if the lock's state cannot be read.
        """
        result = self._run_lock_command(
            "status", "--config", self._config_dir, "--json"
        )
        if result.returncode != 0:
            msg = f"`cluster status` failed (exit {result.returncode})"
            raise BootRefused(msg)
        try:
            return result.json()["state_observations"]
        except (ValueError, KeyError) as exc:
            msg = f"`cluster status` printed no lock state: {result.stdout[:200]!r}"
            raise BootRefused(msg) from exc

    def plan(self) -> dict[str, Any]:
        """The plan, which reads the root without taking the writer lock.

        A plan that finds problems exits non-zero and still prints its JSON.
        """
        result = self._run("plan", "--config", self._config_dir, *self._as, "--json")
        try:
            return result.json()
        except ValueError as exc:
            msg = f"`cluster plan` printed no plan (exit {result.returncode})"
            raise BootRefused(msg) from exc

    def apply(self) -> None:
        result = self._run("apply", "--config", self._config_dir, *self._as, "--json")
        if result.returncode != 0:
            sys.stdout.write(result.stdout)
            msg = f"`cluster apply` failed (exit {result.returncode})"
            raise BootRefused(msg)

    def force_unlock(self, lock_id: str) -> None:
        self._lease.confirm(UNLOCK_BUDGET_SECONDS)
        result = self._run_lock_command(
            "force-unlock", "--config", self._config_dir, lock_id
        )
        if result.returncode != 0:
            msg = f"`cluster force-unlock {lock_id}` failed (exit {result.returncode})"
            raise BootRefused(msg)
        sys.stdout.write(result.stdout)


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


def check_plan(plan: dict[str, Any], removable_graphs: frozenset[str]) -> None:
    """Refuse a plan the apply must not act on.

    :param removable_graphs: graph ids the deployment has explicitly allowed
        to be deleted. From 0.13 on, applying a `cluster.yaml` that no longer
        declares a graph deletes its storage and history, and the generated
        `cluster.yaml` reaches this pod through a ConfigMap that a failed
        deploy does not roll back.
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
    if not plan.get("ok"):
        msg = f"`cluster plan` refused the configuration: {json.dumps(diagnostics)}"
        raise BootRefused(msg)


def release_previous_lock(cluster: Cluster) -> None:
    """Remove the lock the previous owner of this root left behind.

    Only called once the Lease is held, no other server pod is live and the
    plan reports no outstanding deployment, so a `serve` or `deployment` lock
    belongs to a process that has exited. A `graph_operation` lock is a
    maintenance run's, which is not in the pod list, so it is never taken
    over here.
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
    """Remove the `deployment` lock this boot's own apply just left."""
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
) -> Cluster:
    """Plan the baked configuration against the root, refusing a bad plan.

    Reads the root and changes nothing in it, so the pre-deploy gate
    (omnigraph_plan_gate.py) runs exactly this beside a serving pod.

    :returns: the cluster the plan was made for, ready to apply.
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
    removable = frozenset(
        graph.strip()
        for graph in env.get("OMNIGRAPH_GRAPH_REMOVAL_ALLOWLIST", "").split(",")
        if graph.strip()
    )
    cluster = Cluster(config_dir, actor, lease)
    check_plan(cluster.plan(), removable)
    return cluster


def converge(
    config_dir: Path, env: dict[str, str], lease: UnlockLease | NoPeers
) -> None:
    """Bring the cluster root to the baked configuration, unlocked."""
    cluster = checked_plan(config_dir, env, lease)
    release_previous_lock(cluster)
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
        float(env.get("OMNIGRAPH_PEER_WAIT_SECONDS", DEFAULT_PEER_WAIT_SECONDS)),
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
        # cluster.yaml to converge. Nothing releases a lock a previous
        # server left on that root.
        log(f"--cluster is a storage URI ({cluster_arg}); skipping cluster bootstrap")
    else:
        try:
            lease = take_over(env)
            converge(Path(cluster_arg), env, lease)
            lease.hand_over()
        except BootRefused as refusal:
            print(f"omnigraph-server: refusing to start: {refusal}", file=sys.stderr)
            sys.exit(1)
        log("cluster converged; starting server")
    sys.stdout.flush()
    os.execvp(SERVER_BINARY, [SERVER_BINARY, *argv])


if __name__ == "__main__":
    main(sys.argv[1:])
