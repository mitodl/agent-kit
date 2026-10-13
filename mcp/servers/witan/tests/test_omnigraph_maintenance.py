"""Tests for docker/omnigraph_maintenance.py and docker/omnigraph_plan_gate.py.

The maintenance window stops the data tier every night and removes cluster
locks, so the cases that matter are the ways it ends: the server is back, or
it is deliberately left down (a migration was armed, or the window's own lock
could not be removed), and no lock is removed that the window did not own.

The last section runs a window against the real binary and is skipped until
the pinned omnigraph has persisted locks (storage format 14).
"""

from __future__ import annotations

import importlib.util
import os
import signal
import sys
import time
from datetime import UTC, datetime

import pytest
import yaml

from .test_omnigraph_server_entrypoint import (
    LEASE,
    REPO_ROOT,
    FakeKubernetes,
    FakeOmnigraph,
    cluster_dir,  # noqa: F401  (fixture)
    entrypoint,
    pod,
    real_env,
    real_lock,
    requires_omnigraph_server,
    requires_persisted_locks,
    serve_until_locked_then_stop,
)

BootRefused = entrypoint.BootRefused
KubernetesError = entrypoint.KubernetesError


def _load(name: str):
    # The scripts import the entrypoint by the name it is installed under,
    # which the entrypoint's own test module has already registered.
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "docker" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


maintenance = _load("omnigraph_maintenance")
plan_gate = _load("omnigraph_plan_gate")

DEPLOYMENT = "omnigraph-server"
SERVER_POD = "omnigraph-server-7d9f-abcde"
JOB_POD = "omnigraph-maintenance-28801-xk2p"
ARMED = maintenance.MIGRATION_ARMED_ANNOTATION
GRAPHS = ["council", "code-bridge"]
ROOT = "s3://ol-data-witan-ci/fmt14"


class FakeCluster(FakeKubernetes):
    """The Lease and pods, plus the server Deployment and its scale."""

    def __init__(self) -> None:
        super().__init__()
        self.pods = [pod(SERVER_POD)]
        self.replicas = 1
        self.annotations: dict[str, str] = {}
        self.deployment_version = 10
        self.scaled: list[int] = []
        self.pods_stop = True
        self.before_scale = None
        self.after_scale = None
        self.match_labels = {"app.kubernetes.io/name": "omnigraph-server"}

    def get_deployment(self, name: str) -> dict:
        assert name == DEPLOYMENT
        return {
            "metadata": {
                "name": name,
                "resourceVersion": str(self.deployment_version),
                "annotations": dict(self.annotations),
            },
            "spec": {
                "replicas": self.replicas,
                "selector": {"matchLabels": dict(self.match_labels)},
            },
        }

    def replace_scale(self, name: str, replicas: int, resource_version: str) -> dict:
        assert name == DEPLOYMENT
        if self.before_scale:
            hook, self.before_scale = self.before_scale, None
            hook()
        if resource_version != str(self.deployment_version):
            raise KubernetesError(409, "the object has been modified")
        self.deployment_version += 1
        self.replicas = replicas
        self.scaled.append(replicas)
        if replicas == 0 and self.pods_stop:
            self.pods = []
        if self.after_scale:
            hook, self.after_scale = self.after_scale, None
            hook()
        return {}

    def arm_migration(self) -> None:
        """Pulumi's arming update: replicas 0 and the annotation, together."""
        self.deployment_version += 1
        self.replicas = 0
        self.annotations[ARMED] = "true"


class FakeCli(FakeOmnigraph):
    """`FakeOmnigraph` plus `optimize` and `cleanup`, which keep a lock too."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.maintained: list[list[str]] = []
        self.fails: set[str] = set()
        self.during: dict[str, object] = {}
        self.status_output: str | None = None
        self._op_ids = iter(f"01GRAPHOP{n}" for n in range(1, 50))

    def __call__(
        self, args: list[str], lease, timeout_seconds=None, interrupt=None
    ) -> object:
        if args[0] not in ("optimize", "cleanup"):
            if self.status_output is not None and args[1] == "status":
                self.calls.append(args[1:])
                return entrypoint.CommandResult(0, self.status_output)
            return super().__call__(args, lease)
        graph = args[args.index("--graph") + 1]
        assert args[args.index("--cluster") + 1] == ROOT
        if self.lock:
            return self._result(1, {"error": "state_lock_held"})
        self.maintained.append([args[0], graph, *args[5:]])
        self.lock = {"id": next(self._op_ids), "operation": "graph_operation"}
        hook = self.during.pop(graph, None)
        if hook:
            hook()
        if interrupt:
            # The real one polls this about once a second while the command runs.
            interrupt()
        return self._result(1 if graph in self.fails else 0, {})


@pytest.fixture
def kube(monkeypatch) -> FakeCluster:
    fake = FakeCluster()
    monkeypatch.setattr(entrypoint.KubernetesApi, "in_cluster", lambda: fake)
    monkeypatch.setattr(entrypoint, "PEER_POLL_SECONDS", 0)
    monkeypatch.setattr(maintenance, "LEASE_WAIT_SECONDS", 0)
    monkeypatch.setattr(maintenance, "LEASE_RETRY_SECONDS", 0)
    return fake


@pytest.fixture
def cli(monkeypatch) -> FakeCli:
    fake = FakeCli(lock={"id": "01SERVE", "operation": "serve"})
    monkeypatch.setattr(entrypoint, "run_omnigraph", fake)
    return fake


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    (tmp_path / "cluster.yaml").write_text(
        yaml.safe_dump(
            {"storage": ROOT, "graphs": {graph: {} for graph in GRAPHS}},
            sort_keys=False,
        )
    )
    return {
        "OMNIGRAPH_CLUSTER_DIR": str(tmp_path),
        "OMNIGRAPH_SERVER_DEPLOYMENT": DEPLOYMENT,
        "OMNIGRAPH_UNLOCK_LEASE": LEASE,
        "POD_NAME": JOB_POD,
        "OMNIGRAPH_CLEANUP_OLDER_THAN": "30d",
        "OMNIGRAPH_WINDOW_SECONDS": "1500",
        "OMNIGRAPH_SERVER_STOP_SECONDS": "0",
    }


# ── A whole window ───────────────────────────────────────────────────────────


def test_a_window_stops_the_server_maintains_every_graph_and_restores(kube, cli, env):
    assert maintenance.main(env) == 0
    assert kube.scaled == [0, 1]
    assert cli.maintained == [["optimize", "council"], ["optimize", "code-bridge"]]
    assert cli.unlocked == ["01SERVE", "01GRAPHOP1", "01GRAPHOP2"]
    assert cli.lock is None
    assert kube.spec["holderIdentity"] == ""


def test_the_recorded_replica_count_is_what_is_restored(kube, cli, env):
    kube.replicas = 2
    assert maintenance.main(env) == 0
    assert kube.scaled == [0, 2]


def test_cleanup_follows_optimize_on_its_day(kube, cli, env):
    today = str(datetime.now(UTC).isoweekday())
    assert maintenance.main({**env, "OMNIGRAPH_CLEANUP_ISO_WEEKDAY": today}) == 0
    retention = ["--older-than", "30d", "--confirm", "--yes"]
    assert cli.maintained[:2] == [["optimize", "council"], ["optimize", "code-bridge"]]
    assert sorted(cli.maintained[2:]) == [
        ["cleanup", "code-bridge", *retention],
        ["cleanup", "council", *retention],
    ]
    assert cli.lock is None


def test_the_cleanup_pass_starts_one_graph_further_each_week(
    kube, cli, env, monkeypatch
):
    """A window that keeps running out must not skip the same graphs weekly."""

    class Week(datetime):
        week = 0

        @classmethod
        def now(cls, tz=None):
            # 2026-01-05 is the Monday of ISO week 2.
            return datetime(2026, 1, 5 + 7 * cls.week, tzinfo=tz)

    monkeypatch.setattr(maintenance, "datetime", Week)
    first_cleaned = []
    for Week.week in range(2):
        cli.maintained.clear()
        cli.lock = {"id": f"01SERVE{Week.week}", "operation": "serve"}
        kube.pods = [pod(SERVER_POD)]
        assert maintenance.main({**env, "OMNIGRAPH_CLEANUP_ISO_WEEKDAY": "1"}) == 0
        first_cleaned.append(cli.maintained[2][1])
    assert sorted(first_cleaned) == sorted(GRAPHS)


def test_cleanup_does_not_run_on_another_day(kube, cli, env):
    other_day = str(datetime.now(UTC).isoweekday() % 7 + 1)
    assert maintenance.main({**env, "OMNIGRAPH_CLEANUP_ISO_WEEKDAY": other_day}) == 0
    assert [step[0] for step in cli.maintained] == ["optimize", "optimize"]


# ── Windows that do not open ─────────────────────────────────────────────────


def test_an_armed_migration_skips_the_window(kube, cli, env):
    kube.annotations[ARMED] = "true"
    assert maintenance.main(env) == 0
    assert kube.scaled == []
    assert kube.spec == {}
    assert cli.calls == []


def test_a_deployment_already_at_zero_skips_the_window(kube, cli, env):
    kube.replicas = 0
    assert maintenance.main(env) == 0
    assert kube.scaled == []
    assert kube.spec == {}


def test_a_lease_still_held_after_the_wait_fails_the_run(kube, cli, env):
    kube.hold_as(f"{SERVER_POD}#1f", renewed_ago=5)
    assert maintenance.main(env) == 1
    assert kube.scaled == []
    assert kube.spec["holderIdentity"] == f"{SERVER_POD}#1f"
    assert cli.calls == []


def test_the_window_waits_out_the_lease_of_a_server_that_just_booted(
    kube, cli, env, monkeypatch
):
    """The entrypoint does not release the Lease at exec; it expires."""
    monkeypatch.setattr(maintenance, "LEASE_WAIT_SECONDS", 30)
    kube.hold_as(f"{SERVER_POD}#1f", renewed_ago=64.9)
    assert maintenance.main(env) == 0
    assert kube.scaled == [0, 1]


def test_a_selector_this_cannot_render_exactly_opens_no_window(kube, cli, env):
    """A selector that missed the pods would show a running server as stopped."""
    kube.match_labels = {}
    assert maintenance.main(env) == 1
    assert kube.scaled == []
    assert kube.spec == {}
    assert cli.calls == []


def test_a_deployment_changed_since_it_was_read_is_not_scaled(kube, cli, env):
    kube.before_scale = kube.arm_migration
    assert maintenance.main(env) == 1
    assert kube.scaled == []
    assert cli.calls == []
    assert kube.spec["holderIdentity"] == ""


# ── Windows cut short ────────────────────────────────────────────────────────


def test_a_failed_graph_does_not_cost_the_others_their_maintenance(kube, cli, env):
    cli.fails = {"council"}
    assert maintenance.main(env) == 1
    assert [step[1] for step in cli.maintained] == GRAPHS
    assert cli.lock is None
    assert kube.scaled == [0, 1]


def test_a_server_that_does_not_stop_keeps_its_lock_and_is_scaled_back(kube, cli, env):
    kube.pods_stop = False
    assert maintenance.main(env) == 1
    assert cli.unlocked == []
    assert cli.maintained == []
    assert kube.scaled == [0, 1]


@pytest.mark.parametrize("operation", ["deployment", "graph_operation"])
def test_a_lock_the_server_did_not_leave_is_never_removed(kube, cli, env, operation):
    """Not on the way in, and not by the close either."""
    cli.lock = {"id": "01OTHER", "operation": operation}
    assert maintenance.main(env) == 1
    assert cli.unlocked == []
    assert cli.maintained == []
    assert cli.lock == {"id": "01OTHER", "operation": operation}
    assert kube.scaled == [0, 1]


def test_a_scale_down_whose_response_was_lost_is_still_restored(kube, cli, env):
    def lose_response() -> None:
        raise KubernetesError(None, "timed out")

    kube.after_scale = lose_response
    assert maintenance.main(env) == 1
    assert kube.scaled == [0, 1]
    assert cli.unlocked == []


def test_a_window_past_its_deadline_starts_nothing_and_restores(kube, cli, env):
    assert maintenance.main({**env, "OMNIGRAPH_WINDOW_SECONDS": "0"}) == 1
    assert cli.maintained == []
    assert cli.unlocked == ["01SERVE"]
    assert kube.scaled == [0, 1]


def test_sigterm_mid_command_releases_that_commands_lock_and_restores(kube, cli, env):
    cli.during["council"] = lambda: os.kill(os.getpid(), signal.SIGTERM)
    assert maintenance.main(env) == 1
    assert cli.maintained == [["optimize", "council"]]
    assert cli.unlocked == ["01SERVE", "01GRAPHOP1"]
    assert cli.lock is None
    assert kube.scaled == [0, 1]
    assert kube.spec["holderIdentity"] == ""


def test_a_lost_lease_leaves_the_lock_and_the_server_down(kube, cli, env):
    """Nothing shows the lock is still this window's, so nothing removes it."""
    cli.during["council"] = lambda: kube.hold_as("operator#aa", renewed_ago=0)
    assert maintenance.main(env) == 1
    assert cli.unlocked == ["01SERVE"]
    assert cli.lock == {"id": "01GRAPHOP1", "operation": "graph_operation"}
    assert kube.scaled == [0]
    assert kube.spec["holderIdentity"] == "operator#aa"


def test_sigterm_before_the_scale_down_stops_nothing(kube, cli, env, monkeypatch):
    acquire = entrypoint.UnlockLease.acquire

    def acquire_then_sigterm(self) -> None:
        acquire(self)
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(entrypoint.UnlockLease, "acquire", acquire_then_sigterm)
    assert maintenance.main(env) == 1
    assert kube.scaled == []
    assert cli.calls == []
    assert kube.spec["holderIdentity"] == ""


def test_an_unreadable_lock_state_at_close_leaves_the_server_down(kube, cli, env):
    """Logged and exited 1, with the Lease released, not a traceback."""

    def break_status() -> None:
        cli.status_output = "<html>503</html>"
        raise maintenance.WindowTerminated

    cli.during["council"] = break_status
    assert maintenance.main(env) == 1
    assert kube.scaled == [0]
    assert kube.spec["holderIdentity"] == ""


# ── The restore ──────────────────────────────────────────────────────────────


def test_a_migration_armed_during_the_window_is_not_scaled_back_up(kube, cli, env):
    cli.during["council"] = kube.arm_migration
    assert maintenance.main(env) == 0
    assert kube.scaled == [0]
    assert kube.replicas == 0
    assert cli.lock is None


def test_an_arm_between_the_restores_read_and_write_is_seen(kube, cli, env):
    cli.during["code-bridge"] = lambda: setattr(
        kube, "before_scale", kube.arm_migration
    )
    assert maintenance.main(env) == 0
    assert kube.scaled == [0]
    assert kube.replicas == 0


def test_a_deployment_someone_else_scaled_up_is_left_alone(kube, cli, env):
    def scale_up() -> None:
        kube.deployment_version += 1
        kube.replicas = 3

    cli.during["code-bridge"] = scale_up
    assert maintenance.main(env) == 0
    assert kube.scaled == [0]
    assert kube.replicas == 3


def test_the_restore_retries_an_unreachable_api(kube, cli, env):
    def fail_once() -> None:
        raise KubernetesError(None, "connection refused")

    cli.during["code-bridge"] = lambda: setattr(kube, "before_scale", fail_once)
    assert maintenance.main(env) == 0
    assert kube.scaled == [0, 1]


def test_a_restore_that_never_succeeds_fails_the_run(kube, cli, env, monkeypatch):
    """The Job must not report success with the server still down."""
    monkeypatch.setattr(maintenance, "RESTORE_ATTEMPTS", 3)
    real_scale = kube.replace_scale

    def refuse_scale_up(name: str, replicas: int, resource_version: str) -> dict:
        if replicas:
            raise KubernetesError(None, "connection refused")
        return real_scale(name, replicas, resource_version)

    kube.replace_scale = refuse_scale_up
    assert maintenance.main(env) == 1
    assert kube.scaled == [0]
    assert cli.lock is None


# ── Running a command ────────────────────────────────────────────────────────


def test_a_command_is_stopped_when_its_interrupt_raises(tmp_path, monkeypatch):
    """What SIGTERM does to a running optimize: the child exits first."""
    stopped = tmp_path / "stopped"
    fake = tmp_path / "omnigraph"
    fake.write_text(
        f"#!/bin/sh\ntrap 'touch {stopped}; exit 143' TERM\nsleep 60 &\nwait\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")

    def interrupt() -> None:
        raise maintenance.WindowTerminated

    started = time.monotonic()
    with pytest.raises(maintenance.WindowTerminated):
        entrypoint.run_omnigraph(
            ["optimize"], entrypoint.NoPeers(may_unlock=True), interrupt=interrupt
        )
    assert stopped.exists()
    assert time.monotonic() - started < 10


def test_a_command_past_its_timeout_is_stopped(tmp_path, monkeypatch):
    stopped = tmp_path / "stopped"
    fake = tmp_path / "omnigraph"
    fake.write_text(
        f"#!/bin/sh\ntrap 'touch {stopped}; exit 143' TERM\nsleep 60 &\nwait\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    started = time.monotonic()
    with pytest.raises(entrypoint.CommandTimedOut, match="did not finish in 1s"):
        entrypoint.run_omnigraph(
            ["optimize"], entrypoint.NoPeers(may_unlock=True), timeout_seconds=1
        )
    assert stopped.exists()
    assert time.monotonic() - started < 10


def test_the_storage_root_defaults_to_the_config_directory(tmp_path):
    (tmp_path / "cluster.yaml").write_text(yaml.safe_dump({"graphs": {"memory": {}}}))
    assert maintenance.cluster_layout(tmp_path) == (str(tmp_path), ["memory"])


# ── The plan gate ────────────────────────────────────────────────────────────


@pytest.fixture
def gate_cli(monkeypatch):
    def install(**kwargs) -> FakeOmnigraph:
        fake = FakeOmnigraph(**kwargs)
        monkeypatch.setattr(entrypoint, "run_omnigraph", fake)
        return fake

    return install


def test_the_gate_passes_a_plan_the_boot_would_apply(gate_cli, tmp_path):
    fake = gate_cli(lock={"id": "01SERVE", "operation": "serve"})
    assert plan_gate.main({"OMNIGRAPH_CLUSTER_DIR": str(tmp_path)}) == 0
    assert fake.commands == ["plan"]
    assert fake.lock == {"id": "01SERVE", "operation": "serve"}


def test_the_gate_fails_on_a_graph_removal_that_is_not_allow_listed(gate_cli, tmp_path):
    removal = {
        "ok": True,
        "diagnostics": [],
        "changes": [{"resource": "graph.bridge", "operation": "delete"}],
    }
    fake = gate_cli(plan=removal)
    env = {"OMNIGRAPH_CLUSTER_DIR": str(tmp_path)}
    assert plan_gate.main(env) == 1
    assert plan_gate.main({**env, "OMNIGRAPH_GRAPH_REMOVAL_ALLOWLIST": "bridge"}) == 0
    assert fake.commands == ["plan", "plan"]


# ── The real binary ──────────────────────────────────────────────────────────


@requires_persisted_locks
@requires_omnigraph_server
def test_real_window_takes_the_serve_lock_maintains_and_leaves_no_lock(
    cluster_dir,  # noqa: F811
    monkeypatch,
):
    env = real_env(cluster_dir)
    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    serve_until_locked_then_stop(cluster_dir, env)
    assert real_lock(cluster_dir)["lock_operation"] == "serve"

    kube = FakeCluster()
    monkeypatch.setattr(entrypoint.KubernetesApi, "in_cluster", lambda: kube)
    status = maintenance.main(
        {
            "OMNIGRAPH_CLUSTER_DIR": str(cluster_dir),
            "OMNIGRAPH_SERVER_DEPLOYMENT": DEPLOYMENT,
            "OMNIGRAPH_UNLOCK_LEASE": LEASE,
            "POD_NAME": JOB_POD,
            "OMNIGRAPH_CLEANUP_OLDER_THAN": "30d",
            "OMNIGRAPH_CLEANUP_ISO_WEEKDAY": str(datetime.now(UTC).isoweekday()),
            "OMNIGRAPH_WINDOW_SECONDS": "300",
        }
    )
    assert status == 0
    assert kube.scaled == [0, 1]
    assert real_lock(cluster_dir)["locked"] is False
    # The server boots on the root the window left.
    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    serve_until_locked_then_stop(cluster_dir, env)


@requires_persisted_locks
@requires_omnigraph_server
def test_real_gate_reads_a_served_root_without_touching_its_lock(
    cluster_dir,  # noqa: F811
):
    env = real_env(cluster_dir)
    entrypoint.converge(cluster_dir, env, entrypoint.NoPeers(may_unlock=True))
    serve_until_locked_then_stop(cluster_dir, env)
    held = real_lock(cluster_dir)

    assert plan_gate.main({**env, "OMNIGRAPH_CLUSTER_DIR": str(cluster_dir)}) == 0
    assert real_lock(cluster_dir)["lock_id"] == held["lock_id"]

    schema = cluster_dir / "schema" / "stub.pg"
    schema.write_text("node Node {\n  slug: String @key\n  note: String\n}\n")
    assert plan_gate.main({**env, "OMNIGRAPH_CLUSTER_DIR": str(cluster_dir)}) == 1
    assert real_lock(cluster_dir)["lock_id"] == held["lock_id"]
