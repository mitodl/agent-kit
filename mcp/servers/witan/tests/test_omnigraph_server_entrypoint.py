"""Tests for docker/omnigraph_server_entrypoint.py — the data tier's boot path.

The entrypoint removes other processes' cluster locks. Unlocking under a live
writer is the one thing here that loses data, so most of these cases are the
refusals: someone else holds the Lease, another pod is still alive, the Lease
was lost before the unlock, the lock is one this boot must not take over.

The plan and status payloads are the shapes the released 0.13.0 binary printed
(2026-10-09). The scripted CLI speaks both dialects, 0.13's `--config`/`--cluster`
and 0.14's `--target`, and every boot case runs under each. The last section
runs the sequence against whichever real binary is on PATH and is skipped
until it has persisted locks (storage format 14).
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
POLICY_DIR = REPO_ROOT / "mcp" / "servers" / "witan" / "policy"
_spec = importlib.util.spec_from_file_location(
    "omnigraph_server_entrypoint",
    REPO_ROOT / "docker" / "omnigraph_server_entrypoint.py",
)
assert _spec and _spec.loader
entrypoint = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = entrypoint
_spec.loader.exec_module(entrypoint)

BootRefused = entrypoint.BootRefused
KubernetesError = entrypoint.KubernetesError

LEASE = "omnigraph-unlock"
POD = "omnigraph-server-7d9f-abcde"
SELECTOR = "app.kubernetes.io/name=omnigraph-server"
ROOT = "s3://ol-data-witan-ci/fmt14"


def micro_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FakeKubernetes:
    """A Lease with optimistic concurrency, and a pod list."""

    def __init__(self) -> None:
        self.version = 1
        self.spec: dict = {}
        self.pods: list[dict] = [pod(POD)]
        self.unreachable = False

    def _lease(self) -> dict:
        return {
            "metadata": {"name": LEASE, "resourceVersion": str(self.version)},
            "spec": dict(self.spec),
        }

    def get_lease(self, name: str) -> dict:
        assert name == LEASE
        if self.unreachable:
            raise KubernetesError(None, "connection refused")
        return self._lease()

    def replace_lease(self, name: str, lease: dict) -> dict:
        assert name == LEASE
        if self.unreachable:
            raise KubernetesError(None, "connection refused")
        if lease["metadata"]["resourceVersion"] != str(self.version):
            raise KubernetesError(409, "the object has been modified")
        self.version += 1
        self.spec = dict(lease["spec"])
        return self._lease()

    def list_pods(self, label_selector: str) -> list[dict]:
        assert label_selector == SELECTOR
        return self.pods

    def hold_as(self, holder: str, *, renewed_ago: float, duration: int = 60) -> None:
        """Another process's write: the holder changes and so does the version."""
        self.version += 1
        self.spec = {
            "holderIdentity": holder,
            "leaseDurationSeconds": duration,
            "renewTime": micro_time(datetime.now(UTC) - timedelta(seconds=renewed_ago)),
            "leaseTransitions": 3,
        }


def pod(name: str, phase: str = "Running", *, terminating: bool = False) -> dict:
    metadata = {"name": name}
    if terminating:
        metadata["deletionTimestamp"] = "2026-10-09T03:20:00Z"
    return {"metadata": metadata, "status": {"phase": phase}}


@pytest.fixture
def kube() -> FakeKubernetes:
    return FakeKubernetes()


@pytest.fixture
def lease(kube):
    held = entrypoint.UnlockLease(kube, LEASE, POD)
    yield held
    held._stop.set()


# ── The Lease ────────────────────────────────────────────────────────────────


def test_takes_an_unheld_lease(kube, lease):
    lease.acquire()
    assert kube.spec["holderIdentity"] == lease.identity
    assert lease.identity.startswith(f"{POD}#")
    assert kube.spec["leaseDurationSeconds"] == entrypoint.LEASE_DURATION_SECONDS
    assert kube.spec["leaseTransitions"] == 1
    assert not lease.lost()


def test_refuses_a_lease_someone_else_validly_holds(kube, lease):
    kube.hold_as("omnigraph-maintenance-28801-xk2p#1f", renewed_ago=10)
    with pytest.raises(BootRefused, match="held by omnigraph-maintenance"):
        lease.acquire()
    assert kube.spec["holderIdentity"] == "omnigraph-maintenance-28801-xk2p#1f"


def test_takes_over_an_expired_lease(kube, lease):
    kube.hold_as("omnigraph-maintenance-28801-xk2p#1f", renewed_ago=600)
    lease.acquire()
    assert kube.spec["holderIdentity"] == lease.identity
    assert kube.spec["leaseTransitions"] == 4


def test_a_lease_inside_the_clock_skew_margin_is_still_held(kube, lease):
    kube.hold_as("other-pod#aa", renewed_ago=62, duration=60)
    with pytest.raises(BootRefused):
        lease.acquire()


def test_takes_over_its_own_pods_earlier_container(kube, lease):
    """A crash-looping entrypoint must not wait out its own previous Lease."""
    kube.hold_as(f"{POD}#deadbeef", renewed_ago=1)
    lease.acquire()
    assert kube.spec["holderIdentity"] == lease.identity


def test_a_holder_with_no_renew_time_is_not_taken_over(kube, lease):
    kube.version += 1
    kube.spec = {"holderIdentity": "someone"}
    with pytest.raises(BootRefused, match="expires unknown"):
        lease.acquire()


def test_refuses_when_the_lease_cannot_be_read(kube, lease):
    kube.unreachable = True
    with pytest.raises(BootRefused, match="cannot read"):
        lease.acquire()


def test_losing_the_race_to_take_the_lease_refuses(kube, lease, monkeypatch):
    real_get = kube.get_lease

    def get_then_lose(name):
        read = real_get(name)
        kube.hold_as("other-pod#aa", renewed_ago=0)
        return read

    monkeypatch.setattr(kube, "get_lease", get_then_lose)
    with pytest.raises(BootRefused, match="could not take"):
        lease.acquire()
    assert kube.spec["holderIdentity"] == "other-pod#aa"


def test_confirm_renews_the_lease(kube, lease):
    lease.acquire()
    before = kube.version
    lease.confirm(entrypoint.UNLOCK_BUDGET_SECONDS)
    assert kube.version == before + 1


def test_confirm_refuses_once_another_holder_has_taken_the_lease(kube, lease):
    lease.acquire()
    kube.hold_as("other-pod#aa", renewed_ago=0)
    with pytest.raises(BootRefused, match="lost"):
        lease.confirm(entrypoint.UNLOCK_BUDGET_SECONDS)
    assert lease.lost()
    assert kube.spec["holderIdentity"] == "other-pod#aa"


def test_a_metadata_only_change_is_not_a_loss(kube, lease):
    """Pulumi relabelling the Lease moves resourceVersion, not the holder."""
    lease.acquire()
    kube.version += 1
    lease.confirm(entrypoint.UNLOCK_BUDGET_SECONDS)
    assert not lease.lost()
    assert kube.spec["holderIdentity"] == lease.identity


def test_ownership_is_lost_when_renewals_stop_succeeding(kube, lease):
    lease.acquire()
    kube.unreachable = True
    assert not lease.lost()
    lease._renewed_at -= entrypoint.LEASE_RENEW_DEADLINE_SECONDS + 1
    assert lease.lost()
    with pytest.raises(BootRefused, match="lost"):
        lease.confirm(entrypoint.UNLOCK_BUDGET_SECONDS)


def test_confirm_refuses_without_enough_lease_left_for_the_unlock(kube, lease):
    lease.acquire()
    with pytest.raises(BootRefused, match="too close to expiry"):
        lease.confirm(entrypoint.LEASE_DURATION_SECONDS)


def test_the_lease_outlives_a_stopped_child():
    """Loss is declared, the child is stopped, and only then can it expire."""
    assert (
        entrypoint.LEASE_DURATION_SECONDS - entrypoint.LEASE_RENEW_DEADLINE_SECONDS
        > entrypoint.CHILD_STOP_SECONDS + entrypoint.LEASE_CLOCK_SKEW_SECONDS
    )
    assert entrypoint.LEASE_RENEW_INTERVAL_SECONDS * 2 < (
        entrypoint.LEASE_RENEW_DEADLINE_SECONDS
    )


# ── The pod check ────────────────────────────────────────────────────────────


def wait_for_peers(kube, lease, wait_seconds=0.0):
    entrypoint.wait_for_peers_to_exit(kube, SELECTOR, POD, lease, wait_seconds)


def test_no_other_pod_passes(kube, lease):
    lease.acquire()
    wait_for_peers(kube, lease)


@pytest.mark.parametrize(
    "other",
    [
        pod("omnigraph-server-7d9f-old"),
        pod("omnigraph-server-7d9f-old", terminating=True),
        pod("omnigraph-server-7d9f-old", "Pending"),
        pod("omnigraph-server-7d9f-old", "Unknown"),
    ],
    ids=["running", "terminating", "pending", "lost-node"],
)
def test_another_live_pod_refuses(kube, lease, other):
    lease.acquire()
    kube.pods.append(other)
    with pytest.raises(BootRefused, match="still live"):
        wait_for_peers(kube, lease)


@pytest.mark.parametrize("phase", ["Failed", "Succeeded"])
def test_a_pod_whose_containers_have_exited_does_not_block(kube, lease, phase):
    """An evicted pod stays in Failed until pod garbage collection."""
    lease.acquire()
    kube.pods.append(pod("omnigraph-server-7d9f-evicted", phase))
    wait_for_peers(kube, lease)


def test_waits_for_a_terminating_pod_to_go(kube, lease, monkeypatch):
    lease.acquire()
    kube.pods.append(pod("omnigraph-server-7d9f-old", terminating=True))
    monkeypatch.setattr(entrypoint.time, "sleep", lambda _: kube.pods.pop())
    wait_for_peers(kube, lease, wait_seconds=30)
    assert [p["metadata"]["name"] for p in kube.pods] == [POD]


def test_a_selector_that_misses_this_pod_refuses(kube, lease):
    lease.acquire()
    kube.pods = [pod("unrelated-pod")]
    with pytest.raises(BootRefused, match="does not match this pod"):
        wait_for_peers(kube, lease)


def test_the_pod_wait_stops_when_the_lease_is_lost(kube, lease):
    lease.acquire()
    kube.pods.append(pod("omnigraph-server-7d9f-old"))
    kube.hold_as("other-pod#aa", renewed_ago=0)
    lease._renew()
    with pytest.raises(BootRefused, match="lost the unlock Lease"):
        wait_for_peers(kube, lease, wait_seconds=30)


# ── The plan guard ───────────────────────────────────────────────────────────

OUTSTANDING_PLAN = {
    "ok": False,
    "diagnostics": [
        {
            "code": "cluster_deployment_outstanding",
            "severity": "error",
            "path": "__cluster/state.json",
            "message": "original deployment remains outstanding",
        }
    ],
    "changes": [
        {"resource": "graph.bridge", "operation": "create", "disposition": "blocked"}
    ],
}


def removal_plan(*, ok: bool) -> dict:
    return {
        "ok": ok,
        "diagnostics": [],
        "changes": [
            {
                "resource": "graph.code-bridge",
                "operation": "delete",
                "disposition": "applied" if ok else "blocked",
                "delete_root": "s3://bucket/fmt14/graphs/code-bridge.omni",
            },
            {"resource": "schema.code-bridge", "operation": "delete"},
            {"resource": "schema.council", "operation": "update"},
        ],
    }


def test_an_outstanding_deployment_refuses():
    with pytest.raises(BootRefused, match="still outstanding"):
        entrypoint.check_plan(OUTSTANDING_PLAN, frozenset())


@pytest.mark.parametrize("ok", [True, False])
def test_a_graph_removal_that_is_not_allow_listed_refuses(ok):
    with pytest.raises(BootRefused, match=r"no longer declares \['code-bridge'\]"):
        entrypoint.check_plan(removal_plan(ok=ok), frozenset({"code-other"}))


def test_an_allow_listed_graph_removal_passes():
    entrypoint.check_plan(removal_plan(ok=True), frozenset({"code-bridge"}))


def test_a_plan_with_errors_refuses():
    plan = {
        "ok": False,
        "diagnostics": [{"code": "schema_parse_error", "severity": "error"}],
        "changes": [],
    }
    with pytest.raises(BootRefused, match="schema_parse_error"):
        entrypoint.check_plan(plan, frozenset())


def test_a_converged_plan_passes():
    entrypoint.check_plan({"ok": True, "diagnostics": [], "changes": []}, frozenset())


# ── The boot sequence, against a scripted CLI ────────────────────────────────


@dataclass(frozen=True)
class Dialect:
    """Which CLI the scripted `omnigraph` is, and the root it is pointed at."""

    targets: bool
    config_dir: Path

    @property
    def lock_command(self) -> str:
        return "plan" if self.targets else "status"


@pytest.fixture(params=["0.13", "0.14"])
def dialect(request, monkeypatch, tmp_path) -> Dialect:
    """Both CLI dialects, against `cluster.yaml` naming an S3 root."""
    targets = request.param == "0.14"
    monkeypatch.setattr(entrypoint, "cli_speaks_targets", lambda: targets)
    (tmp_path / "cluster.yaml").write_text(
        yaml.safe_dump({"storage": ROOT, "graphs": {"council": {}}})
    )
    return Dialect(targets, tmp_path)


class FakeServed:
    """A server's branch API over ``fake``'s branches.

    A branch in ``fake.sigterm_during`` raises SIGTERM while its delete runs,
    and the delete still finishes, as an HTTP request in flight does. One in
    ``fake.delete_fails`` is refused.
    """

    base_url = "http://127.0.0.1:18080"

    def __init__(self, fake: FakeOmnigraph) -> None:
        self.fake = fake

    def branches(self, graph: str) -> list[str]:
        if self.fake.unavailable:
            raise entrypoint.ServerUnavailable("connection refused")
        if not graph.startswith(entrypoint.CODE_GRAPH_PREFIX):
            # The view-drop actor holds the reaper's grants, on code graphs only.
            raise BootRefused(f"GET /graphs/{graph}/branches: HTTP 403")
        return sorted(self.fake.branches.get(graph, []))

    def delete(self, graph: str, branch: str, timeout: float) -> None:
        assert timeout > 0
        if self.fake.unavailable:
            raise entrypoint.ServerUnavailable("connection refused")
        if branch in self.fake.delete_fails:
            raise BootRefused(f"DELETE {branch}: HTTP 409")
        if branch in self.fake.sigterm_during:
            signal.raise_signal(signal.SIGTERM)
        self.fake.branches[graph].remove(branch)
        self.fake.deleted.append((graph, branch))


class FakeOmnigraph:
    """`omnigraph cluster …` with 0.13's to 0.15's lock behaviour, and the
    loopback server the boot drops branch views through.

    apply leaves a lock, a second writer is refused while any lock exists, and
    force-unlock removes exactly the id it is given. The loopback server takes
    the `serve` lock and releases it on a clean exit, unless
    ``loopback_leaves_lock`` says it had to be killed. Every call is checked
    against the argument shape of ``dialect``.

    Without an explicit ``plan``, the plan is 0.15's: a schema change to any
    graph in ``schema_changes`` is refused while one of them has a branch
    besides main, and then every one of them is marked blocked. Once none has,
    the plan is ``hidden_refusal`` if one is set, else it passes.
    """

    def __init__(
        self,
        dialect: Dialect,
        lock: dict | None = None,
        plan: dict | None = None,
        branches: dict[str, list[str]] | None = None,
        schema_changes: list[str] | None = None,
    ) -> None:
        self.dialect = dialect
        self.lock = lock
        self.plan = plan
        self.branches = {
            graph: list(names) for graph, names in (branches or {}).items()
        }
        self.schema_changes = schema_changes or []
        self.hidden_refusal: dict | None = None
        self.delete_fails: set[str] = set()
        # Branches whose delete is still running when SIGTERM arrives.
        self.sigterm_during: set[str] = set()
        self.unavailable = False
        self.loopback_leaves_lock = False
        self.deleted: list[tuple[str, str]] = []
        self.apply_fails = False
        self.state_found = True
        self.calls: list[list[str]] = []
        self._ids = iter(f"01APPLY{n}" for n in range(1, 10))

    def current_plan(self) -> dict:
        if self.plan is not None:
            return self.plan
        branched = [graph for graph in self.schema_changes if self.branches.get(graph)]
        if not branched:
            return self.hidden_refusal or {"ok": True, "diagnostics": [], "changes": []}
        first = ", ".join(sorted(self.branches[branched[0]]))
        return {
            "ok": False,
            "diagnostics": [
                {
                    "code": "schema_preflight_failed",
                    "severity": "error",
                    "path": "__cluster/state.json",
                    "message": (
                        "schema apply requires a graph with only main; found "
                        f"non-main branches: {first}"
                    ),
                }
            ],
            "changes": [
                {
                    "resource": f"{kind}.{graph}",
                    "operation": "update",
                    "disposition": "blocked",
                    "reason": "schema_preflight_failed",
                }
                for kind in ("graph", "schema")
                for graph in self.schema_changes
            ],
        }

    @contextlib.contextmanager
    def loopback(self, config_dir, env, lease, terminated) -> Iterator[FakeServed]:
        assert self.lock is None, f"the loopback server started on {self.lock}"
        self.calls.append(["loopback"])
        self.lock = {"id": "01LOOPBACK", "operation": "serve"}
        try:
            yield FakeServed(self)
        finally:
            if not self.loopback_leaves_lock:
                self.lock = None

    def check_shape(self, args: list[str]) -> None:
        config = ["--config", str(self.dialect.config_dir)]
        target = ["--target", ROOT]
        command, rest = args[0], args[1:]
        if not self.dialect.targets:
            assert "--target" not in args, args
            assert rest[:2] == config, args
        elif command == "force-unlock":
            assert rest == [*target, rest[-1]], args
        elif command == "status":
            # 0.15's status reports the lock id but not its operation.
            assert rest == [*target, "--json"], args
        else:
            assert rest[:4] == [*config, *target], args

    def observed(self) -> dict:
        observed = {"state_found": self.state_found, "locked": self.lock is not None}
        if self.lock:
            observed |= {
                "lock_id": self.lock["id"],
                "lock_operation": self.lock["operation"],
            }
        return observed

    def __call__(
        self,
        args: list[str],
        lease,
        timeout_seconds=None,
        interrupt=None,
        terminated=None,
    ) -> object:
        assert args[0] == "cluster"
        self.calls.append(args[1:])
        self.check_shape(args[1:])
        command = args[1]
        if command == "status" and self.dialect.targets:
            return self._result(0, {"lock_id": self.lock and self.lock["id"]})
        if command == "status":
            return self._result(0, {"ok": True, "state_observations": self.observed()})
        if command == "plan":
            current = self.current_plan()
            plan = {**current, "state_observations": self.observed()}
            return self._result(0 if current["ok"] else 1, plan)
        if command == "apply":
            if self.lock or self.apply_fails:
                return self._result(1, {"ok": False})
            self.lock = {"id": next(self._ids), "operation": "deployment"}
            return self._result(0, {"status": "complete"})
        if command == "force-unlock":
            if not self.lock or self.lock["id"] != args[-1]:
                return self._result(1, {})
            self.lock = None
            return self._result(0, {})
        raise AssertionError(args)

    @staticmethod
    def _result(returncode: int, payload: dict):
        return entrypoint.CommandResult(returncode, json.dumps(payload))

    @property
    def commands(self) -> list[str]:
        return [call[0] for call in self.calls]

    @property
    def unlocked(self) -> list[str]:
        return [call[-1] for call in self.calls if call[0] == "force-unlock"]


@pytest.fixture
def cli(monkeypatch, dialect):
    def install(**kwargs) -> FakeOmnigraph:
        fake = FakeOmnigraph(dialect, **kwargs)
        monkeypatch.setattr(entrypoint, "run_omnigraph", fake)
        monkeypatch.setattr(entrypoint, "loopback_server", fake.loopback)
        return fake

    return install


class LostLease(entrypoint.NoPeers):
    def confirm(self, budget_seconds: float) -> None:
        raise BootRefused("lost the Lease; not unlocking")


def converge(config_dir: Path, lease=None, **env: str) -> None:
    entrypoint.converge(config_dir, env, lease or entrypoint.NoPeers(may_unlock=True))


def test_a_fresh_root_is_applied_and_left_unlocked(cli, tmp_path):
    fake = cli()
    converge(tmp_path)
    lock = fake.dialect.lock_command
    assert fake.commands == ["plan", lock, "apply", lock, "force-unlock"]
    assert fake.unlocked == ["01APPLY1"]
    assert fake.lock is None


@pytest.mark.parametrize("operation", ["serve", "deployment"])
def test_the_previous_owners_lock_is_released_before_apply(cli, tmp_path, operation):
    fake = cli(lock={"id": "01PREVIOUS", "operation": operation})
    converge(tmp_path)
    assert fake.unlocked == ["01PREVIOUS", "01APPLY1"]
    assert fake.commands.index("force-unlock") < fake.commands.index("apply")
    assert fake.lock is None


@pytest.mark.parametrize("operation", ["graph_operation", "deployment_reconcile"])
def test_a_lock_this_boot_must_not_take_over_refuses(cli, tmp_path, operation):
    fake = cli(lock={"id": "01MAINTENANCE", "operation": operation})
    with pytest.raises(BootRefused, match=operation):
        converge(tmp_path)
    assert fake.unlocked == []
    assert "apply" not in fake.commands


def test_an_outstanding_deployment_is_not_unlocked(cli, tmp_path):
    fake = cli(
        lock={"id": "01INTERRUPTED", "operation": "deployment"}, plan=OUTSTANDING_PLAN
    )
    with pytest.raises(BootRefused, match="still outstanding"):
        converge(tmp_path)
    assert fake.commands == ["plan"]
    assert fake.lock == {"id": "01INTERRUPTED", "operation": "deployment"}


def test_a_refused_graph_removal_is_not_applied(cli, tmp_path):
    fake = cli(plan=removal_plan(ok=True))
    with pytest.raises(BootRefused, match="code-bridge"):
        converge(tmp_path)
    assert fake.commands == ["plan"]


def test_an_allow_listed_graph_removal_is_applied(cli, tmp_path):
    fake = cli(plan=removal_plan(ok=True))
    converge(tmp_path, OMNIGRAPH_GRAPH_REMOVAL_ALLOWLIST="council-old, code-bridge")
    assert "apply" in fake.commands


def test_nothing_is_unlocked_once_the_lease_is_lost(cli, tmp_path):
    fake = cli(lock={"id": "01PREVIOUS", "operation": "serve"})
    with pytest.raises(BootRefused, match="not unlocking"):
        converge(tmp_path, lease=LostLease(may_unlock=True))
    assert fake.unlocked == []
    assert "apply" not in fake.commands
    assert fake.lock == {"id": "01PREVIOUS", "operation": "serve"}


def test_a_failed_apply_refuses(cli, tmp_path):
    fake = cli()
    fake.apply_fails = True
    with pytest.raises(BootRefused, match="apply"):
        converge(tmp_path)
    assert fake.commands == ["plan", fake.dialect.lock_command, "apply"]


def test_the_apply_actor_is_passed_to_plan_and_apply(cli, tmp_path):
    fake = cli()
    converge(tmp_path, OMNIGRAPH_APPLY_ACTOR="svc-witan-admin")
    for call in fake.calls:
        if call[0] == "apply" or call[0] == "plan" and call is fake.calls[0]:
            assert call[-3:-1] == ["--as", "svc-witan-admin"]
        else:
            assert "--as" not in call


def test_bundles_without_a_token_map_refuse(cli, tmp_path):
    fake = cli()
    (tmp_path / "memory.policy.yaml").write_text("version: 1\n")
    with pytest.raises(BootRefused, match="no bearer-token map"):
        converge(tmp_path, OMNIGRAPH_APPLY_ACTOR="svc-witan-admin")
    assert fake.calls == []


def test_bundles_without_an_apply_actor_refuse(cli, tmp_path, monkeypatch):
    fake = cli()
    monkeypatch.setattr(
        entrypoint, "RENDER_SCRIPT", str(POLICY_DIR / "render_groups.py")
    )
    shutil.copy(POLICY_DIR / "memory.policy.yaml", tmp_path)
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps(RENDER_TOKENS))
    with pytest.raises(BootRefused, match="OMNIGRAPH_APPLY_ACTOR"):
        converge(tmp_path, OMNIGRAPH_SERVER_BEARER_TOKENS_FILE=str(tokens))
    assert fake.calls == []


# ── Branch views and schema changes ──────────────────────────────────────────

CODE_GRAPH = "code-github-com-mitodl-agent-kit"
VIEWS = {
    CODE_GRAPH: ["act-0ebb3a7b/_detached", "act-0ebb3a7b/feat_docstrings"],
    "code-bridge": ["act-5c1d/main"],
}
DROPPABLE = f"{CODE_GRAPH},code-bridge"


@pytest.fixture
def code_cli(cli, dialect):
    """The scripted CLI, against a cluster with the council and two code graphs."""
    (dialect.config_dir / "cluster.yaml").write_text(
        yaml.safe_dump(
            {
                "storage": ROOT,
                "graphs": {
                    "council": {"schema": "./schema.pg"},
                    "code-bridge": {"schema": "./bridge-schema.pg"},
                    CODE_GRAPH: {"schema": "./code-schema.pg"},
                },
            }
        )
    )

    def install(**kwargs) -> FakeOmnigraph:
        kwargs.setdefault("branches", VIEWS)
        kwargs.setdefault("schema_changes", [CODE_GRAPH, "code-bridge"])
        return cli(**kwargs)

    return install


def test_code_graph_views_are_dropped_before_the_apply(code_cli, tmp_path):
    fake = code_cli(lock={"id": "01PREVIOUS", "operation": "serve"})
    converge(
        tmp_path,
        OMNIGRAPH_APPLY_ACTOR="svc-witan-admin",
        OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE,
    )
    assert sorted(fake.deleted) == sorted(
        (graph, branch) for graph, branches in VIEWS.items() for branch in branches
    )
    assert fake.branches == {CODE_GRAPH: [], "code-bridge": []}
    # The previous owner's lock goes first, then the loopback server and its
    # deletes, then a plan with nothing refused, then the apply.
    commands = fake.commands
    assert commands.index("force-unlock") < commands.index("loopback")
    assert commands.index("loopback") < commands.index("apply")
    assert fake.unlocked == ["01PREVIOUS", "01APPLY1"]
    assert fake.lock is None


def test_a_council_schema_change_with_council_branches_still_refuses(
    code_cli, tmp_path
):
    """The view-drop actor cannot read council, so its branches show up as the
    refusal that outlasts the drop. The gate finds this while the old pod
    still serves."""
    fake = code_cli(
        branches={**VIEWS, "council": ["wip"]},
        schema_changes=["council", CODE_GRAPH, "code-bridge"],
        lock={"id": "01PREVIOUS", "operation": "serve"},
    )
    with pytest.raises(BootRefused, match=r"still block.*'council'"):
        converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE)
    assert "apply" not in fake.commands
    assert fake.branches["council"] == ["wip"]
    assert fake.lock is None


def test_council_branches_with_no_views_left_refuse_before_serving(code_cli, tmp_path):
    fake = code_cli(
        branches={"council": ["wip"]},
        schema_changes=["council", CODE_GRAPH, "code-bridge"],
    )
    with pytest.raises(BootRefused, match=r"of \['council'\].*one has others"):
        converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE)
    assert fake.deleted == []
    assert "apply" not in fake.commands


def test_council_branches_do_not_matter_without_a_council_schema_change(
    code_cli, tmp_path
):
    fake = code_cli(branches={**VIEWS, "council": ["wip"]})
    converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE)
    assert fake.branches["council"] == ["wip"]
    assert "apply" in fake.commands


def test_views_on_a_graph_that_is_not_allow_listed_refuse(code_cli, tmp_path):
    fake = code_cli()
    with pytest.raises(BootRefused, match="OMNIGRAPH_BRANCH_DROP_ALLOWLIST"):
        converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=CODE_GRAPH)
    assert fake.branches["code-bridge"] == VIEWS["code-bridge"]
    assert "apply" not in fake.commands


def test_without_an_allow_list_the_branch_refusal_stands(code_cli, tmp_path):
    """No graph's views may go, so nothing is served on loopback to list them."""
    fake = code_cli()
    with pytest.raises(BootRefused, match="only main"):
        converge(tmp_path)
    assert fake.commands == ["plan"]


def test_views_are_kept_when_no_schema_change_needs_them_gone(code_cli, tmp_path):
    """Every restart plans; only a schema change may cost the views."""
    fake = code_cli(schema_changes=[])
    converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE)
    assert "loopback" not in fake.commands
    assert fake.branches == VIEWS
    assert "apply" in fake.commands


def test_an_allow_list_naming_an_undeclared_graph_refuses(code_cli, tmp_path):
    fake = code_cli()
    with pytest.raises(BootRefused, match=r"names \['code-typo'\]"):
        converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=f"{DROPPABLE},code-typo")
    assert fake.calls == []


def test_a_branch_refusal_beside_another_error_drops_nothing(code_cli, tmp_path):
    fake = code_cli()
    plan = fake.current_plan()
    plan["diagnostics"].append({"code": "schema_parse_error", "severity": "error"})
    fake.plan = plan
    with pytest.raises(BootRefused, match="schema_parse_error"):
        converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE)
    assert "loopback" not in fake.commands


def test_another_preflight_refusal_drops_nothing(code_cli, tmp_path):
    fake = code_cli()
    plan = fake.current_plan()
    plan["diagnostics"][0]["message"] = "schema apply cannot drop a column"
    fake.plan = plan
    with pytest.raises(BootRefused, match="cannot drop a column"):
        converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE)
    assert "loopback" not in fake.commands


def test_a_refusal_behind_the_views_stops_the_boot_after_the_drop(code_cli, tmp_path):
    """The branch refusal can hide another; the views stay dropped."""
    fake = code_cli()
    fake.hidden_refusal = {
        "ok": False,
        "diagnostics": [{"code": "schema_migration_unsupported", "severity": "error"}],
        "changes": [],
    }
    with pytest.raises(BootRefused, match="schema_migration_unsupported"):
        converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE)
    assert fake.branches == {CODE_GRAPH: [], "code-bridge": []}
    assert "apply" not in fake.commands
    assert fake.lock is None


def test_a_failed_delete_stops_the_boot_unlocked_and_the_next_boot_finishes(
    code_cli, tmp_path
):
    fake = code_cli()
    fake.delete_fails = {"act-0ebb3a7b/feat_docstrings"}
    with pytest.raises(BootRefused, match="feat_docstrings"):
        converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE)
    # The deletes before it stay done, and the loopback server's lock is gone.
    assert fake.branches["code-bridge"] == []
    assert fake.branches[CODE_GRAPH] == ["act-0ebb3a7b/feat_docstrings"]
    assert fake.lock is None
    assert "apply" not in fake.commands

    fake.delete_fails = set()
    converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE)
    assert fake.branches == {CODE_GRAPH: [], "code-bridge": []}
    assert "apply" in fake.commands
    assert fake.lock is None


def test_a_killed_loopback_servers_lock_is_released_before_the_apply(
    code_cli, tmp_path
):
    fake = code_cli()
    fake.loopback_leaves_lock = True
    converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE)
    assert fake.unlocked == ["01LOOPBACK", "01APPLY1"]
    assert fake.lock is None


class LostDuringDrop(entrypoint.NoPeers):
    def lost(self) -> bool:
        return True


def test_a_lost_lease_stops_the_drop_before_any_delete(code_cli, tmp_path):
    fake = code_cli()
    with pytest.raises(BootRefused, match="lost the unlock Lease"):
        converge(
            tmp_path,
            lease=LostDuringDrop(may_unlock=True),
            OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE,
        )
    assert fake.deleted == []
    assert "apply" not in fake.commands


def test_sigterm_mid_delete_stops_the_drop_with_the_root_unlocked(code_cli, tmp_path):
    """The startupProbe's kill on a long drop must not strand a lock. The
    delete in flight finishes; the next one does not start."""
    fake = code_cli()
    fake.sigterm_during = {"act-0ebb3a7b/_detached"}
    previous = signal.getsignal(signal.SIGTERM)
    env = {"OMNIGRAPH_BRANCH_DROP_ALLOWLIST": DROPPABLE}
    lease = entrypoint.NoPeers(may_unlock=True)
    with (
        pytest.raises(BootRefused, match="SIGTERM"),
        entrypoint.sigterm_noted() as terminated,
    ):
        entrypoint.converge(tmp_path, env, lease, terminated)
    assert signal.getsignal(signal.SIGTERM) is previous
    assert fake.lock is None
    assert fake.branches["code-bridge"] == []
    assert fake.branches[CODE_GRAPH] == ["act-0ebb3a7b/feat_docstrings"]
    assert "apply" not in fake.commands

    fake.sigterm_during = set()
    converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=DROPPABLE)
    assert fake.branches == {CODE_GRAPH: [], "code-bridge": []}
    assert "apply" in fake.commands


def test_sigterm_before_the_drop_stops_the_boot_before_any_delete(code_cli, tmp_path):
    """A rollout or drain that lands while the boot is still planning."""
    fake = code_cli()
    env = {"OMNIGRAPH_BRANCH_DROP_ALLOWLIST": DROPPABLE}
    lease = entrypoint.NoPeers(may_unlock=True)
    with (
        pytest.raises(BootRefused, match="SIGTERM"),
        entrypoint.sigterm_noted() as terminated,
    ):
        signal.raise_signal(signal.SIGTERM)
        entrypoint.converge(tmp_path, env, lease, terminated)
    assert fake.deleted == []
    assert fake.lock is None
    assert "apply" not in fake.commands


def test_sigterm_as_the_last_delete_ends_stops_the_boot_before_the_apply(
    code_cli, tmp_path
):
    """A last delete that returns after SIGTERM leaves nothing for the drop to
    stop."""
    fake = code_cli()
    fake.sigterm_during = {"act-0ebb3a7b/feat_docstrings"}
    env = {"OMNIGRAPH_BRANCH_DROP_ALLOWLIST": DROPPABLE}
    lease = entrypoint.NoPeers(may_unlock=True)
    with (
        pytest.raises(BootRefused, match="SIGTERM after dropping the branch views"),
        entrypoint.sigterm_noted() as terminated,
    ):
        entrypoint.converge(tmp_path, env, lease, terminated)
    assert fake.branches == {CODE_GRAPH: [], "code-bridge": []}
    assert fake.lock is None
    assert "apply" not in fake.commands


def test_sigterm_during_the_plan_after_the_drop_stops_the_boot_before_the_apply(
    code_cli, tmp_path, monkeypatch
):
    fake = code_cli()

    def run(args, lease, **kwargs):
        result = fake(args, lease, **kwargs)
        if args[:2] == ["cluster", "plan"] and not any(fake.branches.values()):
            signal.raise_signal(signal.SIGTERM)
        return result

    monkeypatch.setattr(entrypoint, "run_omnigraph", run)
    env = {"OMNIGRAPH_BRANCH_DROP_ALLOWLIST": DROPPABLE}
    with (
        pytest.raises(BootRefused, match="SIGTERM while planning after the drop"),
        entrypoint.sigterm_noted() as terminated,
    ):
        entrypoint.converge(
            tmp_path, env, entrypoint.NoPeers(may_unlock=True), terminated
        )
    assert fake.commands[-1] == "plan"
    assert fake.lock is None
    assert "apply" not in fake.commands


def test_the_cleanup_budget_is_the_grace_less_the_margin():
    margin = entrypoint.SIGTERM_CLEANUP_MARGIN_SECONDS
    assert entrypoint.sigterm_cleanup_seconds({}) == (
        entrypoint.DEFAULT_TERMINATION_GRACE_SECONDS - margin
    )
    env = {"OMNIGRAPH_TERMINATION_GRACE_SECONDS": "65"}
    assert entrypoint.sigterm_cleanup_seconds(env) == 65 - margin
    env = {"OMNIGRAPH_TERMINATION_GRACE_SECONDS": "30"}
    with pytest.raises(BootRefused, match="leaves no time"):
        entrypoint.sigterm_cleanup_seconds(env)


def test_the_peer_wait_outlasts_the_termination_grace():
    """Another server pod gets the whole grace to exit before the boot gives up."""
    margin = entrypoint.PEER_EXIT_MARGIN_SECONDS
    assert entrypoint.peer_wait_seconds({}) == (
        entrypoint.DEFAULT_TERMINATION_GRACE_SECONDS + margin
    )
    env = {"OMNIGRAPH_TERMINATION_GRACE_SECONDS": "200"}
    assert entrypoint.peer_wait_seconds(env) == 200 + margin
    env["OMNIGRAPH_PEER_WAIT_SECONDS"] = "40"
    assert entrypoint.peer_wait_seconds(env) == 40


@pytest.mark.parametrize("graph", ["council", "bridge"])
def test_an_allow_list_naming_a_graph_that_is_not_a_code_graph_refuses(
    code_cli, tmp_path, graph
):
    fake = code_cli()
    with pytest.raises(BootRefused, match="not code graphs"):
        converge(tmp_path, OMNIGRAPH_BRANCH_DROP_ALLOWLIST=f"{DROPPABLE},{graph}")
    assert fake.calls == []


def test_the_cluster_dir_comes_from_the_servers_own_arguments():
    argv = ["--cluster", "/srv/cluster", "--bind", "0.0.0.0:8080"]
    assert entrypoint.cluster_argument(argv, {}) == "/srv/cluster"
    assert entrypoint.cluster_argument(["--cluster=s3://b/root"], {}) == "s3://b/root"
    assert (
        entrypoint.cluster_argument(["--bind", "x"], {"OMNIGRAPH_CLUSTER_DIR": "/e"})
        == "/e"
    )
    assert entrypoint.cluster_argument([], {}) == entrypoint.DEFAULT_CLUSTER_DIR


def test_take_over_outside_kubernetes_skips_the_lease():
    assert isinstance(entrypoint.take_over({}), entrypoint.NoPeers)


def test_outside_kubernetes_nothing_is_unlocked_without_the_opt_in(cli, tmp_path):
    fake = cli(lock={"id": "01PREVIOUS", "operation": "serve"})
    with pytest.raises(BootRefused, match="OMNIGRAPH_UNLOCK_WITHOUT_PEERS"):
        converge(tmp_path, lease=entrypoint.take_over({}))
    assert "force-unlock" not in fake.commands
    assert "apply" not in fake.commands


def test_outside_kubernetes_the_opt_in_allows_the_unlock(cli, tmp_path):
    fake = cli(lock={"id": "01PREVIOUS", "operation": "serve"})
    lease = entrypoint.take_over({"OMNIGRAPH_UNLOCK_WITHOUT_PEERS": "1"})
    converge(tmp_path, lease=lease)
    assert "force-unlock" in fake.commands


def test_take_over_in_kubernetes_requires_the_lease_and_selector(monkeypatch, kube):
    monkeypatch.setattr(entrypoint.KubernetesApi, "in_cluster", lambda: kube)
    env = {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "HOSTNAME": POD}
    with pytest.raises(KeyError, match="OMNIGRAPH_UNLOCK_LEASE"):
        entrypoint.take_over(env)
    env["OMNIGRAPH_UNLOCK_LEASE"] = LEASE
    with pytest.raises(KeyError, match="OMNIGRAPH_POD_SELECTOR"):
        entrypoint.take_over(env)
    env["OMNIGRAPH_POD_SELECTOR"] = SELECTOR
    held = entrypoint.take_over(env)
    try:
        assert kube.spec["holderIdentity"] == held.identity
    finally:
        held.hand_over()


def test_a_running_command_is_stopped_when_the_lease_is_lost(tmp_path, monkeypatch):
    """The child's I/O has to be over before the Lease can expire."""
    stopped = tmp_path / "stopped"
    fake = tmp_path / "omnigraph"
    fake.write_text(
        f"#!/bin/sh\ntrap 'touch {stopped}; exit 143' TERM\nsleep 60 &\nwait\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")

    class Lost(entrypoint.NoPeers):
        def lost(self) -> bool:
            return True

    started = time.monotonic()
    with pytest.raises(BootRefused, match="lost the unlock Lease"):
        entrypoint.run_omnigraph(["cluster", "apply"], Lost(may_unlock=True))
    assert stopped.exists()
    assert time.monotonic() - started < 10


FAKE_SERVER = '''#!/usr/bin/env python3
"""omnigraph-server's branch API over two in-memory branches."""
import http.server
import json
import os
import signal
import sys
import urllib.parse

if os.environ.get("EXIT_AT_START"):
    sys.exit(int(os.environ["EXIT_AT_START"]))
if os.environ.get("SLOW_STOP"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
port = int(sys.argv[sys.argv.index("--bind") + 1].rsplit(":", 1)[1])
branches = {"code-x": ["main", "act-a/feat"]}
loading = int(os.environ.get("LOADING_REPLIES", "0"))


class Handler(http.server.BaseHTTPRequestHandler):
    def reply(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def refused(self):
        if self.headers["Omnigraph-Http-Api"] != "0.14":
            self.reply(400, {"code": "api_contract_mismatch"})
            return True
        if self.headers["Authorization"] != "Bearer t3":
            self.reply(401, {"code": "unauthorized"})
            return True
        return False

    def do_GET(self):
        global loading
        if self.path == "/healthz":
            self.reply(200, {"status": "ok"})
        elif self.refused():
            pass
        elif loading > 0:
            # omnigraph-server 0.15 serves /healthz while a graph still loads.
            loading -= 1
            self.reply(
                503,
                {
                    "error": "graph is loading; wait for its startup attempt to complete",
                    "code": "graph_unavailable",
                    "disposition": {"effects": "none", "action": "retry"},
                },
            )
        else:
            graph = self.path.split("/")[2]
            self.reply(200, {"branches": branches[graph]})

    def do_DELETE(self):
        if self.refused():
            return
        if os.environ.get("FAIL_DELETE"):
            self.reply(500, {"code": "storage_error"})
            return
        prefix, _, name = self.path.partition("/branches/")
        listed = branches[prefix.split("/")[2]]
        name = urllib.parse.unquote(name)
        if name not in listed:
            self.reply(404, {"code": "not_found"})
            return
        listed.remove(name)
        self.reply(200, {"name": name})

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


@pytest.fixture
def fake_server(tmp_path, monkeypatch) -> Path:
    """A scripted `omnigraph-server` on PATH, and a config dir with a token map.

    ``EXIT_AT_START`` makes it exit with that code, ``SLOW_STOP`` makes it
    ignore SIGTERM, ``FAIL_DELETE`` makes every delete a 500, and
    ``LOADING_REPLIES`` answers that many branch listings with the 503 a
    graph still loading gets.
    """
    binary = tmp_path / "bin" / entrypoint.SERVER_BINARY
    binary.parent.mkdir()
    binary.write_text(FAKE_SERVER)
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:{os.environ['PATH']}")
    for name in ("EXIT_AT_START", "SLOW_STOP", "FAIL_DELETE", "LOADING_REPLIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(entrypoint, "http_contract", lambda: "0.14")
    monkeypatch.setattr(entrypoint, "SIGTERM_STOP_SECONDS", 1)
    (tmp_path / "cluster.yaml").write_text(
        yaml.safe_dump({"storage": ROOT, "graphs": {"code-x": {}}})
    )
    (tmp_path / "tokens.json").write_text(json.dumps(RENDER_TOKENS))
    return tmp_path


def loopback(config_dir: Path, terminated=None, actor: str = "svc-witan-ci"):
    env = {
        "OMNIGRAPH_SERVER_BEARER_TOKENS_FILE": str(config_dir / "tokens.json"),
        "OMNIGRAPH_VIEW_DROP_ACTOR": actor,
    }
    return entrypoint.loopback_server(
        config_dir,
        env,
        entrypoint.NoPeers(may_unlock=True),
        terminated or entrypoint.Termination(30),
    )


def test_views_are_listed_and_deleted_through_the_loopback_server(fake_server):
    with loopback(fake_server) as served:
        assert served.branches("code-x") == ["act-a/feat"]
        served.delete("code-x", "act-a/feat", 5)
        assert served.branches("code-x") == []
        # Gone already (the reaper got there first) counts as deleted.
        served.delete("code-x", "act-a/feat", 5)
    assert not served.serving()


def test_a_server_error_on_a_delete_is_unavailable(fake_server, monkeypatch):
    monkeypatch.setenv("FAIL_DELETE", "1")
    with (
        loopback(fake_server) as served,
        pytest.raises(entrypoint.ServerUnavailable, match="HTTP 500"),
    ):
        served.delete("code-x", "act-a/feat", 5)


def test_a_graph_still_loading_is_listed_once_it_loads(fake_server, monkeypatch):
    """The loopback server answers /healthz before every graph has loaded.

    Production's boot on 2026-11-19 listed code-github-com-mitodl-agent-kit
    two seconds after the loopback server served, got this 503, and refused
    to start on every restart.
    """
    monkeypatch.setenv("LOADING_REPLIES", "3")
    monkeypatch.setattr(entrypoint, "GRAPH_LOAD_POLL_SECONDS", 0.01)
    with loopback(fake_server) as served:
        assert served.branches("code-x") == ["act-a/feat"]


def test_a_graph_that_never_loads_is_unavailable(fake_server, monkeypatch):
    monkeypatch.setenv("LOADING_REPLIES", "1000")
    monkeypatch.setattr(entrypoint, "GRAPH_LOAD_POLL_SECONDS", 0.01)
    monkeypatch.setattr(entrypoint, "GRAPH_LOAD_SECONDS", 0.2)
    with (
        loopback(fake_server) as served,
        pytest.raises(entrypoint.ServerUnavailable, match="graph_unavailable"),
    ):
        served.branches("code-x")


def test_sigterm_stops_the_wait_for_a_graph_to_load(fake_server, monkeypatch):
    monkeypatch.setenv("LOADING_REPLIES", "1000")
    monkeypatch.setattr(entrypoint, "GRAPH_LOAD_POLL_SECONDS", 0.01)
    terminated = entrypoint.Termination(30)
    with loopback(fake_server, terminated) as served:
        terminated.note()
        started = time.monotonic()
        with pytest.raises(BootRefused, match="SIGTERM while waiting") as refused:
            served.branches("code-x")
        assert not isinstance(refused.value, entrypoint.ServerUnavailable)
        assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'["graph_unavailable"]',
        b'{"code": "graph_unavailable", "disposition": null}',
        b'{"code": "graph_unavailable", "disposition": {"action": "fail"}}',
        b'{"code": "storage_error", "disposition": {"action": "retry"}}',
    ],
)
def test_only_a_loading_graphs_503_is_retried(body):
    assert not entrypoint._says_retry(503, body)


def test_a_loading_graphs_503_is_retried():
    body = b'{"code": "graph_unavailable", "disposition": {"action": "retry"}}'
    assert entrypoint._says_retry(503, body)
    assert not entrypoint._says_retry(500, body)


def test_a_refused_token_refuses_rather_than_waiting_for_the_boot(fake_server):
    """A 4xx is a configuration error, not a pod that is down."""
    with loopback(fake_server, actor="svc-witan-admin") as served:
        with pytest.raises(BootRefused, match="HTTP 401") as refused:
            served.branches("code-x")
        assert not isinstance(refused.value, entrypoint.ServerUnavailable)


def test_an_unreachable_server_is_unavailable(monkeypatch):
    monkeypatch.setattr(entrypoint, "http_contract", lambda: "0.14")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    served = entrypoint.ServedGraphs(f"http://127.0.0.1:{port}", "t3")
    with pytest.raises(entrypoint.ServerUnavailable):
        served.branches("code-x")


def test_a_loopback_server_that_exits_refuses(fake_server, monkeypatch):
    monkeypatch.setenv("EXIT_AT_START", "3")
    with pytest.raises(BootRefused, match=r"exited \(3\) before serving"):
        loopback(fake_server).__enter__()


def test_a_loopback_server_slow_to_stop_after_sigterm_is_killed_in_time(
    fake_server, monkeypatch
):
    """After SIGTERM it gets SIGTERM_STOP_SECONDS, not CHILD_STOP_SECONDS."""
    monkeypatch.setenv("SLOW_STOP", "1")
    terminated = entrypoint.Termination(30)
    with loopback(fake_server, terminated) as served:
        terminated.note()
        started = time.monotonic()
    assert time.monotonic() - started < 3
    assert not served.serving()


# ── The two CLI dialects ─────────────────────────────────────────────────────


@pytest.fixture
def probe(monkeypatch, tmp_path):
    """Put a scripted `omnigraph` first on PATH for the version probe."""

    def install(script: str) -> None:
        binary = tmp_path / "bin" / "omnigraph"
        binary.parent.mkdir()
        binary.write_text(f"#!/bin/sh\n{script}\n")
        binary.chmod(0o755)
        monkeypatch.setenv("PATH", f"{binary.parent}:/usr/bin:/bin")
        entrypoint.cli_speaks_targets.cache_clear()

    yield install
    entrypoint.cli_speaks_targets.cache_clear()


def test_a_cli_without_version_json_speaks_0_13(probe):
    probe("echo \"error: unexpected argument '--json' found\" >&2; exit 2")
    assert entrypoint.cli_speaks_targets() is False


def test_target_protocol_2_speaks_0_14(probe):
    probe("""echo '{"version": "0.14.0", "protocols": {"target": 2}}'""")
    assert entrypoint.cli_speaks_targets() is True


def test_another_target_protocol_refuses(probe):
    probe("""echo '{"version": "0.14.0-edge", "protocols": {"target": 1}}'""")
    with pytest.raises(BootRefused, match="target protocol 1"):
        entrypoint.cli_speaks_targets()


def test_0_14_addresses_the_s3_root_with_target(monkeypatch, tmp_path):
    monkeypatch.setattr(entrypoint, "cli_speaks_targets", lambda: True)
    (tmp_path / "cluster.yaml").write_text(
        yaml.safe_dump({"storage": ROOT, "graphs": {"council": {}}})
    )
    fake = FakeOmnigraph(
        Dialect(True, tmp_path), lock={"id": "01PREVIOUS", "operation": "serve"}
    )
    monkeypatch.setattr(entrypoint, "run_omnigraph", fake)
    converge(tmp_path, OMNIGRAPH_APPLY_ACTOR="svc-witan-admin")
    source = ["--config", str(tmp_path), "--target", ROOT]
    assert fake.calls == [
        ["plan", *source, "--as", "svc-witan-admin", "--json"],
        ["plan", *source, "--json"],
        ["force-unlock", "--target", ROOT, "01PREVIOUS"],
        ["apply", *source, "--as", "svc-witan-admin", "--json"],
        ["plan", *source, "--json"],
        ["force-unlock", "--target", ROOT, "01APPLY1"],
    ]
    cluster = entrypoint.Cluster(tmp_path, None, entrypoint.NoPeers(may_unlock=True))
    assert cluster.graph_command("cleanup", "council", "--confirm", "--yes") == [
        "cleanup",
        "--target",
        ROOT,
        "--graph",
        "council",
        "--confirm",
        "--yes",
    ]


def test_0_13_addresses_a_graph_with_cluster(monkeypatch, tmp_path):
    monkeypatch.setattr(entrypoint, "cli_speaks_targets", lambda: False)
    (tmp_path / "cluster.yaml").write_text(
        yaml.safe_dump({"storage": ROOT, "graphs": {"council": {}}})
    )
    cluster = entrypoint.Cluster(tmp_path, None, entrypoint.NoPeers(may_unlock=True))
    assert cluster.graph_command("optimize", "council") == [
        "optimize",
        "--cluster",
        ROOT,
        "--graph",
        "council",
    ]


def test_the_root_without_storage_is_the_absolute_config_directory(
    monkeypatch, tmp_path
):
    """0.14 reads a bare relative name as a registered target, not a path."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cluster").mkdir()
    (tmp_path / "cluster" / "cluster.yaml").write_text(
        yaml.safe_dump({"graphs": {"memory": {}}})
    )
    assert entrypoint.cluster_layout(Path("cluster")) == (
        str(tmp_path / "cluster"),
        ["memory"],
    )


@pytest.mark.parametrize("storage", ["data", "./data"])
def test_a_relative_storage_targets_the_config_directory(
    monkeypatch, tmp_path, storage
):
    """0.14 resolves a relative `storage` against the config directory, so the
    `--target` must too, from whatever directory the process runs in."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert entrypoint.inline_target(tmp_path / "cluster", storage) == str(
        tmp_path / "cluster" / storage
    )


@pytest.mark.parametrize("root", ["s3://bucket/root", "file:///srv/root", "/srv/root"])
def test_a_uri_or_absolute_storage_is_the_target_unchanged(root):
    assert entrypoint.inline_target(Path("cluster"), root) == root


@pytest.mark.parametrize(
    ("diagnostic", "state_found"),
    [
        # Refused before the root is read: the lock reads as unlocked.
        ({"code": "schema_file_missing", "severity": "error"}, False),
        ({"code": "state_lock_read_error", "severity": "warning"}, True),
    ],
)
def test_a_0_14_lock_read_that_did_not_reach_the_lock_refuses(
    monkeypatch, dialect, diagnostic, state_found
):
    if not dialect.targets:
        pytest.skip("0.13 reads the lock from `cluster status`")
    fake = FakeOmnigraph(
        dialect, plan={"ok": False, "diagnostics": [diagnostic], "changes": []}
    )
    fake.state_found = state_found
    monkeypatch.setattr(entrypoint, "run_omnigraph", fake)
    cluster = entrypoint.Cluster(
        dialect.config_dir, None, entrypoint.NoPeers(may_unlock=True)
    )
    with pytest.raises(BootRefused, match=diagnostic["code"]):
        cluster.lock()


def test_a_0_14_plan_error_past_the_ledger_still_reads_the_lock(monkeypatch, dialect):
    """What a schema change planned without `--as` reports under policy."""
    if not dialect.targets:
        pytest.skip("0.13 reads the lock from `cluster status`")
    no_actor = {"code": "schema_preflight_failed", "severity": "error"}
    fake = FakeOmnigraph(
        dialect,
        lock={"id": "01GRAPHOP", "operation": "graph_operation"},
        plan={"ok": False, "diagnostics": [no_actor], "changes": []},
    )
    monkeypatch.setattr(entrypoint, "run_omnigraph", fake)
    cluster = entrypoint.Cluster(
        dialect.config_dir, None, entrypoint.NoPeers(may_unlock=True)
    )
    assert cluster.lock()["lock_id"] == "01GRAPHOP"


def test_a_target_the_cli_refuses_is_reported(monkeypatch, dialect):
    refusal = {
        "error": "target_invalid: inline access supports local storage only",
        "code": "target_invalid",
    }

    def refuse(args, lease, timeout_seconds=None):
        return entrypoint.CommandResult(2, json.dumps(refusal))

    monkeypatch.setattr(entrypoint, "run_omnigraph", refuse)
    cluster = entrypoint.Cluster(
        dialect.config_dir, None, entrypoint.NoPeers(may_unlock=True)
    )
    with pytest.raises(BootRefused, match="local storage only"):
        cluster.plan()


# ── The real binary ──────────────────────────────────────────────────────────

RENDER_TOKENS = {
    "act-alice": "t1",  # pragma: allowlist secret
    "act-bob": "t2",  # pragma: allowlist secret
    "svc-witan-ci": "t3",  # pragma: allowlist secret
    "svc-witan-admin": "t4",  # pragma: allowlist secret
}


def _has_persisted_locks() -> bool:
    found = shutil.which("omnigraph")
    if found is None:
        return False
    from witan_core.omnigraph_install import _installed_version

    # By release rather than by `version --json`, which 0.14 added: these
    # tests also cover the 0.13 CLI path, and every release prints
    # `--version`. 0.13 is the first pinned release on format 14.
    version = _installed_version(Path(found))
    return version is not None and tuple(map(int, version.split("."))) >= (0, 13, 0)


requires_persisted_locks = pytest.mark.skipif(
    not _has_persisted_locks(),
    reason="needs an omnigraph with persisted cluster locks (0.13 or newer)",
)

# `witan setup` and CI install only the `omnigraph` CLI from the release
# tarball, so the cases that hold a real `serve` lock skip without the server.
requires_omnigraph_server = pytest.mark.skipif(
    shutil.which("omnigraph-server") is None,
    reason="needs omnigraph-server on PATH",
)


@pytest.fixture
def cluster_dir(tmp_path, monkeypatch) -> Path:
    """The policy fixture cluster, as the image lays it out."""
    monkeypatch.setattr(
        entrypoint, "RENDER_SCRIPT", str(POLICY_DIR / "render_groups.py")
    )
    config = tmp_path / "cluster"
    config.mkdir()
    for name in ("cluster.yaml", "find.gq"):
        shutil.copy(POLICY_DIR / name, config)
    for bundle in POLICY_DIR.glob("*.policy.yaml"):
        shutil.copy(bundle, config)
    shutil.copytree(POLICY_DIR / "schema", config / "schema")
    (config / "tokens.json").write_text(json.dumps(RENDER_TOKENS))
    return config


def real_env(config: Path, **extra: str) -> dict[str, str]:
    return {
        "OMNIGRAPH_SERVER_BEARER_TOKENS_FILE": str(config / "tokens.json"),
        "OMNIGRAPH_APPLY_ACTOR": "svc-witan-admin",
        "OMNIGRAPH_VIEW_DROP_ACTOR": "svc-witan-ci",
        **extra,
    }


def real_lock(config: Path) -> dict:
    return entrypoint.Cluster(config, None, entrypoint.NoPeers(may_unlock=True)).lock()


def graph_stores(config: Path) -> set[str]:
    return {path.name for path in (config / "graphs").iterdir()}


def serve_until_locked_then_stop(
    config: Path, env: dict[str, str], *, crash: bool = False
) -> None:
    """Run omnigraph-server until it holds its lock, then SIGTERM it.

    :param crash: SIGKILL it instead. A crashed server leaves its `serve`
        lock on 0.13 and 0.14; a clean shutdown leaves it only on 0.13.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen(
        [
            "omnigraph-server",
            "--cluster",
            str(config),
            "--bind",
            f"127.0.0.1:{port}",
        ],
        env={**env, "PATH": os.environ["PATH"]},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not real_lock(config)["locked"]:
            assert server.poll() is None, "omnigraph-server exited during startup"
            assert time.monotonic() < deadline
            time.sleep(0.2)
    finally:
        if crash:
            server.kill()
        else:
            server.terminate()
        server.wait(timeout=60)


@requires_persisted_locks
def test_real_first_boot_creates_the_graphs_and_leaves_no_lock(cluster_dir):
    entrypoint.converge(
        cluster_dir, real_env(cluster_dir), entrypoint.NoPeers(may_unlock=True)
    )
    assert graph_stores(cluster_dir) == {
        "memory.omni",
        "code-example.omni",
        "bridge.omni",
    }
    assert real_lock(cluster_dir)["locked"] is False


@requires_persisted_locks
@requires_omnigraph_server
def test_real_restart_takes_over_the_serve_lock(cluster_dir):
    """Boot, serve, crash, boot again: the lock handoff this file exists for."""
    env = real_env(cluster_dir)
    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    serve_until_locked_then_stop(cluster_dir, env, crash=True)
    held = real_lock(cluster_dir)
    assert (held["locked"], held["lock_operation"]) == (True, "serve")

    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    assert real_lock(cluster_dir)["locked"] is False


@requires_persisted_locks
@requires_omnigraph_server
def test_real_clean_shutdown_leaves_the_serve_lock_only_on_0_13(cluster_dir):
    """0.14 releases writer admission after a proved clean shutdown."""
    env = real_env(cluster_dir)
    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    serve_until_locked_then_stop(cluster_dir, env)
    assert real_lock(cluster_dir)["locked"] is not entrypoint.cli_speaks_targets()

    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    assert real_lock(cluster_dir)["locked"] is False


@requires_persisted_locks
def test_real_schema_change_is_applied_as_the_admin_actor(cluster_dir):
    env = real_env(cluster_dir)
    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    schema = cluster_dir / "schema" / "stub.pg"
    schema.write_text("node Node {\n  slug: String @key\n  note: String?\n}\n")
    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    plan = entrypoint.Cluster(
        cluster_dir, "svc-witan-admin", entrypoint.NoPeers(may_unlock=True)
    ).plan()
    assert plan["ok"] is True
    assert plan["changes"] == []
    assert real_lock(cluster_dir)["locked"] is False


@requires_persisted_locks
def test_real_dropped_graph_is_refused_and_its_storage_kept(cluster_dir):
    env = real_env(cluster_dir)
    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    manifest = cluster_dir / "cluster.yaml"
    config = yaml.safe_load(manifest.read_text())
    del config["graphs"]["bridge"]
    del config["policies"]["bridge_rules"]
    manifest.write_text(yaml.safe_dump(config))

    with pytest.raises(BootRefused, match=r"no longer declares \['bridge'\]"):
        entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    assert "bridge.omni" in graph_stores(cluster_dir)
    assert real_lock(cluster_dir)["locked"] is False

    entrypoint.converge(
        cluster_dir,
        {**env, "OMNIGRAPH_GRAPH_REMOVAL_ALLOWLIST": "bridge"},
        entrypoint.NoPeers(may_unlock=True),
    )
    assert "bridge.omni" not in graph_stores(cluster_dir)
    assert real_lock(cluster_dir)["locked"] is False


def real_branch(config: Path, graph: str, name: str) -> None:
    """Create a branch view the way a writer would, and remove its lock."""
    cluster = entrypoint.Cluster(config, None, entrypoint.NoPeers(may_unlock=True))
    subprocess.run(
        [
            "omnigraph",
            *cluster.graph_command(
                "branch", graph, "--as", "svc-witan-ci", "create", name, "--json"
            ),
        ],
        check=True,
        capture_output=True,
    )
    # A direct `branch create` keeps its `graph_operation` lock after it exits.
    cluster.force_unlock(cluster.lock()["lock_id"])


def real_branches(config: Path, graph: str) -> list[str]:
    """``graph``'s branches but main, read directly: the root is local."""
    cluster = entrypoint.Cluster(config, None, entrypoint.NoPeers(may_unlock=True))
    listed = subprocess.run(
        ["omnigraph", *cluster.graph_command("branch", graph, "list", "--json")],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(set(json.loads(listed.stdout)["branches"]) - {"main"})


def change_stub_schema(config: Path) -> None:
    """A schema change to every fixture graph, which share one schema file."""
    (config / "schema" / "stub.pg").write_text(
        "node Node {\n  slug: String @key\n  note: String?\n}\n"
    )


@requires_persisted_locks
@requires_omnigraph_server
def test_real_unsupported_migration_behind_the_views_refuses_after_the_drop(
    cluster_dir,
):
    """`cluster plan` reports only the branch refusal here, so the memory
    graph's unsupported type change shows up once the views are gone."""
    manifest = cluster_dir / "cluster.yaml"
    config = yaml.safe_load(manifest.read_text())
    config["graphs"]["memory"]["schema"] = "./schema/memory.pg"
    manifest.write_text(yaml.safe_dump(config))
    memory = cluster_dir / "schema" / "memory.pg"
    memory.write_text("node Node {\n  slug: String @key\n  note: I32?\n}\n")
    env = real_env(cluster_dir, OMNIGRAPH_BRANCH_DROP_ALLOWLIST="code-example")
    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    real_branch(cluster_dir, "code-example", "act-alice/feat_x")
    change_stub_schema(cluster_dir)
    memory.write_text("node Node {\n  slug: String @key\n  note: String?\n}\n")

    with pytest.raises(BootRefused, match=r"refused the configuration.*Node\.note"):
        entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    assert real_branches(cluster_dir, "code-example") == []
    assert real_lock(cluster_dir)["locked"] is False


@requires_persisted_locks
@requires_omnigraph_server
def test_real_code_graph_views_are_dropped_so_the_schema_change_applies(
    cluster_dir,
):
    env = real_env(cluster_dir, OMNIGRAPH_BRANCH_DROP_ALLOWLIST="code-example")
    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    real_branch(cluster_dir, "code-example", "act-alice/feat_x")
    real_branch(cluster_dir, "code-example", "act-alice/_detached")
    change_stub_schema(cluster_dir)

    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    assert real_branches(cluster_dir, "code-example") == []
    plan = entrypoint.Cluster(
        cluster_dir, "svc-witan-admin", entrypoint.NoPeers(may_unlock=True)
    ).plan()
    assert (plan["ok"], plan["changes"]) == (True, [])
    assert real_lock(cluster_dir)["locked"] is False


@requires_persisted_locks
@requires_omnigraph_server
def test_real_memory_graph_branch_still_refuses_the_schema_change(cluster_dir):
    env = real_env(cluster_dir, OMNIGRAPH_BRANCH_DROP_ALLOWLIST="code-example")
    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    real_branch(cluster_dir, "code-example", "act-alice/feat_x")
    real_branch(cluster_dir, "memory", "wip")
    change_stub_schema(cluster_dir)

    with pytest.raises(BootRefused, match=r"still block.*'memory'"):
        entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    assert real_branches(cluster_dir, "memory") == ["wip"]
    assert real_lock(cluster_dir)["locked"] is False


@requires_persisted_locks
def test_real_views_survive_a_boot_with_no_schema_change(cluster_dir):
    env = real_env(cluster_dir, OMNIGRAPH_BRANCH_DROP_ALLOWLIST="code-example")
    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    real_branch(cluster_dir, "code-example", "act-alice/feat_x")

    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    assert real_branches(cluster_dir, "code-example") == ["act-alice/feat_x"]


@requires_persisted_locks
def test_real_interrupted_apply_is_reported_as_outstanding(cluster_dir):
    """Pins the signal D3 left unverified: plan's `cluster_deployment_outstanding`."""
    entrypoint.render_policy_groups(cluster_dir, str(cluster_dir / "tokens.json"))
    target = ["--target", str(cluster_dir)] if entrypoint.cli_speaks_targets() else []
    plan = None
    for attempt in range(40):
        shutil.rmtree(cluster_dir / "__cluster", ignore_errors=True)
        shutil.rmtree(cluster_dir / "graphs", ignore_errors=True)
        apply = subprocess.Popen(
            [
                "omnigraph",
                "cluster",
                "apply",
                "--config",
                str(cluster_dir),
                *target,
                "--json",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.02 + 0.005 * attempt)
        apply.kill()
        apply.wait()
        plan = entrypoint.Cluster(
            cluster_dir, None, entrypoint.NoPeers(may_unlock=True)
        ).plan()
        if not plan["ok"]:
            break
    else:
        pytest.skip("could not interrupt an apply mid-run on this machine")
    assert [d["code"] for d in plan["diagnostics"]] == [
        "cluster_deployment_outstanding"
    ]

    held = real_lock(cluster_dir)
    assert (held["locked"], held["lock_operation"]) == (True, "deployment")
    with pytest.raises(BootRefused, match="still outstanding"):
        entrypoint.converge(
            cluster_dir, real_env(cluster_dir), entrypoint.NoPeers(may_unlock=True)
        )
    assert real_lock(cluster_dir)["lock_id"] == held["lock_id"]
