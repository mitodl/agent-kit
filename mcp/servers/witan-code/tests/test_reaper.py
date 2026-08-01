"""Reaping stale branch views from a shared code graph.

Two halves. The selection rules are pure and exercised against fabricated
ages — that is where the judgement calls live (never `main`, never a view with
no writes of its own, never anything when the window is 0). The rest runs
against a real omnigraph store, because the whole scheme rests on one
non-obvious claim about the binary: that `branch list` gives names only, and a
branch's own last write is recoverable from `commit list --branch` via
`manifest_branch`. If that stops being true the reaper silently ages everything
identically, so it is asserted rather than mocked.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from witan_code import config as cfg_module
from witan_code import reaper as reaper_module
from witan_code.graph import OmnigraphClient
from witan_code.reaper import ViewAge, reap, select_stale

from .conftest import requires_omnigraph

DAY = 86400.0
NOW = 1_800_000_000.0


def _age(view: str, days_idle: float | None) -> ViewAge:
    last = None if days_idle is None else NOW - days_idle * DAY
    return ViewAge(view=view, last_write=last)


def _names(ages) -> list[str]:
    return [a.view for a in ages]


# ── Selection ────────────────────────────────────────────────────────────────


def test_idle_views_past_the_window_are_selected():
    ages = [_age("act-alice/feature-x", 30), _age("act-bob/feature-y", 1)]
    assert _names(select_stale(ages, now=NOW, max_idle=14)) == ["act-alice/feature-x"]


def test_main_is_never_reaped_however_idle():
    """`main` is the committed index every reader falls back to. It is idle by
    design between merges, so idleness must not condemn it."""
    ages = [_age("main", 900)]
    assert select_stale(ages, now=NOW, max_idle=14) == []


def test_a_view_with_no_writes_of_its_own_is_never_reaped():
    """It holds nothing that isn't already on the branch it forked from, and
    there is no creation timestamp to age it by — so deleting it reclaims
    nothing and races whoever just created it."""
    ages = [_age("act-alice/just-created", None)]
    assert select_stale(ages, now=NOW, max_idle=14) == []


def test_a_zero_window_disables_reaping():
    """WITAN_CODE_VIEW_MAX_IDLE_DAYS=0 is the off switch, so the caller doesn't
    have to special-case it before calling."""
    ages = [_age("act-alice/ancient", 900)]
    assert select_stale(ages, now=NOW, max_idle=0) == []


def test_the_window_boundary_is_inclusive():
    assert _names(select_stale([_age("v", 14)], now=NOW, max_idle=14)) == ["v"]
    assert select_stale([_age("v", 13.9)], now=NOW, max_idle=14) == []


def test_stale_views_come_back_oldest_first():
    ages = [_age("recent", 20), _age("ancient", 400), _age("middling", 60)]
    assert _names(select_stale(ages, now=NOW, max_idle=14)) == [
        "ancient",
        "middling",
        "recent",
    ]


def test_a_view_reports_its_owner():
    """The sweep is reported per-owner, so a person can see their own views go."""
    assert _age("act-alice/feature-x", 1).owner == "act-alice"
    assert _age("feature-x", 1).owner is None


# ── The idle window ──────────────────────────────────────────────────────────


def test_max_idle_days_defaults_when_unset(monkeypatch):
    monkeypatch.delenv(reaper_module.MAX_IDLE_ENV_VAR, raising=False)
    assert reaper_module.max_idle_days() == reaper_module.DEFAULT_MAX_IDLE_DAYS


def test_max_idle_days_reads_the_env_var(monkeypatch):
    monkeypatch.setenv(reaper_module.MAX_IDLE_ENV_VAR, "3")
    assert reaper_module.max_idle_days() == 3.0


def test_a_malformed_window_is_an_error_not_a_default(monkeypatch):
    """Silently falling back to 14 days would let a typo'd cron env delete views
    the operator meant to keep for 90."""
    monkeypatch.setenv(reaper_module.MAX_IDLE_ENV_VAR, "two weeks")
    with pytest.raises(ValueError, match="not a number of days"):
        reaper_module.max_idle_days()


# ── Authority ────────────────────────────────────────────────────────────────


def _cfg(role: str = cfg_module.INDEX_ROLE_CLIENT) -> cfg_module.Config:
    return cfg_module.Config(
        code_dir=Path("/code"),
        author="test",
        queries_dir=Path("/queries"),
        schema_file=Path("/schema.pg"),
        bridge_schema_file=Path("/bridge.pg"),
        index_role=role,
    )


class _FakeClient:
    """A graph with one ancient view and one fresh one."""

    def __init__(self, *, is_remote: bool):
        self.is_remote = is_remote
        self.deleted: list[str] = []

    def list_branches(self):
        return ["main", "act-alice/ancient", "act-bob/fresh"]

    def branch_last_write(self, name):
        return NOW - (400 * DAY if name == "act-alice/ancient" else DAY)

    def delete_branch(self, name):
        self.deleted.append(name)


def test_a_shared_graph_is_only_reapable_by_ci():
    """Reaping deletes views this process does not own — exactly what no
    ordinary client may do. Cedar grants branch_delete to witan-ci alone; this
    turns that into a local error instead of a server denial."""
    client = _FakeClient(is_remote=True)
    with pytest.raises(PermissionError, match="WITAN_CODE_INDEX_ROLE=ci"):
        reap(client, graph="code-x", now=NOW, max_idle=14, apply=True, cfg=_cfg())
    assert client.deleted == []


def test_ci_may_reap_a_shared_graph():
    client = _FakeClient(is_remote=True)
    report = reap(
        client,
        graph="code-x",
        now=NOW,
        max_idle=14,
        apply=True,
        cfg=_cfg(cfg_module.INDEX_ROLE_CI),
    )
    assert client.deleted == ["act-alice/ancient"]
    assert report.deleted == ["act-alice/ancient"]


def test_a_local_store_needs_no_role():
    """No policy engine in front of it and one user who owns everything."""
    client = _FakeClient(is_remote=False)
    reap(client, graph="local", now=NOW, max_idle=14, apply=True, cfg=_cfg())
    assert client.deleted == ["act-alice/ancient"]


def test_reporting_a_shared_graph_needs_no_role():
    """The refusal is about deleting, not looking — anyone may see what is
    stale, including to check the window before a job runs with it."""
    client = _FakeClient(is_remote=True)
    report = reap(client, graph="code-x", now=NOW, max_idle=14, cfg=_cfg())
    assert client.deleted == []
    assert [a.view for a in report.stale] == ["act-alice/ancient"]


def test_without_apply_nothing_is_deleted():
    client = _FakeClient(is_remote=False)
    report = reap(client, graph="local", now=NOW, max_idle=14, cfg=_cfg())
    assert client.deleted == []
    assert report.deleted == []
    assert report.scanned == 2  # main is not a view


def test_one_undeletable_view_does_not_strand_the_sweep():
    """A scheduled job that aborts on the first failure never gets past it."""

    class _Stubborn(_FakeClient):
        def list_branches(self):
            return ["act-alice/ancient", "act-bob/older"]

        def branch_last_write(self, name):
            return NOW - 400 * DAY

        def delete_branch(self, name):
            if name == "act-alice/ancient":
                raise RuntimeError("branch is locked")
            super().delete_branch(name)

    report = reap(
        _Stubborn(is_remote=False), graph="local", now=NOW, max_idle=14, apply=True
    )
    assert report.deleted == ["act-bob/older"]
    assert report.failed == [("act-alice/ancient", "branch is locked")]


# ── Against the real binary ──────────────────────────────────────────────────


@requires_omnigraph
def test_last_write_comes_from_the_branchs_own_commits(tmp_path, monkeypatch):
    """The claim the whole scheme rests on: `branch list` gives bare names, and
    a branch's own last write is recoverable from `commit list --branch` by
    filtering on `manifest_branch`. A branch that only inherited its parent's
    commits must read as never-written, or every fresh view would look as old
    as the store."""
    import subprocess
    import time

    store = tmp_path / "t.omni"
    schema = tmp_path / "s.pg"
    schema.write_text("node Node {\n  slug: String @key\n}\n")
    query = tmp_path / "ins.gq"
    query.write_text("query ins($slug: String) {\n    insert Node { slug: $slug }\n}\n")

    binary = OmnigraphClient._find_binary()
    subprocess.run(
        [binary, "init", "--schema", str(schema), str(store)],
        check=True,
        capture_output=True,
    )
    client = OmnigraphClient(str(store), tmp_path)
    for name in ("act-alice/written", "act-bob/untouched"):
        subprocess.run(
            [binary, "branch", "create", "--store", f"file://{store}", name],
            check=True,
            capture_output=True,
        )

    before = time.time()
    OmnigraphClient(str(store), tmp_path, branch="act-alice/written").change(
        "ins.gq", "ins", {"slug": "a"}
    )

    assert set(client.list_branches()) == {
        "main",
        "act-alice/written",
        "act-bob/untouched",
    }
    written = client.branch_last_write("act-alice/written")
    assert written is not None
    assert before - 1 <= written <= time.time() + 1
    assert client.branch_last_write("act-bob/untouched") is None


@requires_omnigraph
def test_the_reaper_deletes_what_it_selected(tmp_path):
    """End to end against a real store: a view aged past the window is gone
    from `branch list` afterwards, and `main` and the fresh view are not."""
    import subprocess

    store = tmp_path / "t.omni"
    schema = tmp_path / "s.pg"
    schema.write_text("node Node {\n  slug: String @key\n}\n")
    query = tmp_path / "ins.gq"
    query.write_text("query ins($slug: String) {\n    insert Node { slug: $slug }\n}\n")

    binary = OmnigraphClient._find_binary()
    subprocess.run(
        [binary, "init", "--schema", str(schema), str(store)],
        check=True,
        capture_output=True,
    )
    for name in ("act-alice/stale", "act-bob/fresh"):
        subprocess.run(
            [binary, "branch", "create", "--store", f"file://{store}", name],
            check=True,
            capture_output=True,
        )
        OmnigraphClient(str(store), tmp_path, branch=name).change(
            "ins.gq", "ins", {"slug": name}
        )

    client = OmnigraphClient(str(store), tmp_path)
    # Both views were just written, so age the clock forward instead of the
    # store: a 30-day-later `now` makes both stale, and a window that only
    # `act-alice/stale` exceeds is unreachable without fabricating timestamps
    # the store has no way to accept.
    stale_at = client.branch_last_write("act-alice/stale")
    report = reap(
        client,
        graph="local",
        now=stale_at + 30 * DAY,
        max_idle=14,
        apply=True,
        cfg=_cfg(),
    )

    assert set(report.deleted) == {"act-alice/stale", "act-bob/fresh"}
    assert client.list_branches() == ["main"]


@requires_omnigraph
def test_a_branch_client_does_not_leak_its_branch_into_commit_list(tmp_path):
    """`commit list` takes its own --branch. A client that injected the one it
    was constructed with would either duplicate the flag or silently answer
    about a different branch than the caller asked about."""
    import subprocess

    store = tmp_path / "t.omni"
    schema = tmp_path / "s.pg"
    schema.write_text("node Node {\n  slug: String @key\n}\n")

    binary = OmnigraphClient._find_binary()
    subprocess.run(
        [binary, "init", "--schema", str(schema), str(store)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [binary, "branch", "create", "--store", f"file://{store}", "other"],
        check=True,
        capture_output=True,
    )
    branched = OmnigraphClient(str(store), tmp_path, branch="other")
    assert branched.branch_last_write("other") is None


def test_survey_skips_protected_views_entirely():
    """Not merely filtered out of the result — never aged. Ageing `main` costs
    a commit-log read per graph per sweep for an answer that cannot matter."""
    asked: list[str] = []

    client = SimpleNamespace(
        list_branches=lambda: ["main", "act-alice/x"],
        branch_last_write=lambda name: asked.append(name) or NOW,
    )
    reaper_module.survey(client, now=NOW, max_idle=14)
    assert asked == ["act-alice/x"]
