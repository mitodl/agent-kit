"""The fan-out policy on WorkflowProject: its validation, its round trip through
the tools and the CLI, and the read a runner uses to decide where to claim."""

from typing import get_args

import pytest

from witan import fanout
from witan.fanout import FanoutPolicy

from .conftest import requires_omnigraph


@pytest.fixture(autouse=True)
def _caller_is_alice(monkeypatch):
    from witan import server as srv

    monkeypatch.setattr(srv, "_current_author", lambda: "alice")


def _row(**fields):
    return {
        "slug": "wp-x",
        "status": "active",
        "author": "alice",
        "fanout_enabled": None,
        "fanout_concurrency": None,
        "fanout_harnesses": None,
        "fanout_labels": None,
        "fanout_set_by": None,
        **fields,
    }


# ── The policy as data ──────────────────────────────────────────────────────


def test_harnesses_match_the_agents_witan_installs_into():
    from witan_core.cli import AGENT_NAMES

    assert set(fanout.HARNESSES) == set(AGENT_NAMES)
    assert fanout.HARNESSES == get_args(fanout.Harness)


def test_a_project_that_never_set_it_is_never_claimed_from():
    policy = FanoutPolicy.of(_row())
    assert not policy.enabled
    assert not policy.admits(harnesses=fanout.HARNESSES, labels=["docker", "gpu"])
    assert fanout.enabled_projects([_row()]) == []


def test_disabled_projects_and_inactive_ones_are_not_listed():
    on = _row(slug="wp-on", fanout_enabled=True)
    off = _row(slug="wp-off", fanout_enabled=False, fanout_concurrency=4)
    done = _row(slug="wp-done", status="completed", fanout_enabled=True)
    assert fanout.enabled_projects([on, off, done]) == [on]


def test_a_row_without_the_fields_fails_rather_than_reading_as_disabled():
    with pytest.raises(KeyError):
        FanoutPolicy.of({"slug": "wp-x", "status": "active"})


def test_unset_concurrency_reads_as_one():
    assert FanoutPolicy.of(_row(fanout_enabled=True)).concurrency == 1


def test_admits_checks_harnesses_and_required_labels():
    policy = FanoutPolicy.of(
        _row(
            fanout_enabled=True,
            fanout_harnesses=["pi", "claude"],
            fanout_labels=["docker"],
        )
    )
    assert policy.admits(harnesses=["claude"], labels=["docker", "gpu"])
    assert not policy.admits(harnesses=["claude"], labels=["gpu"])
    assert not policy.admits(harnesses=["codex"], labels=["docker"])
    assert policy.allowed_harnesses(["claude", "codex", "pi"]) == ("pi", "claude")


def test_no_harness_list_allows_any_the_runner_has():
    policy = FanoutPolicy.of(_row(fanout_enabled=True))
    assert policy.allowed_harnesses(["codex", "pi"]) == ("codex", "pi")
    assert policy.admits(harnesses=["codex"], labels=[])
    assert not policy.admits(harnesses=[], labels=[])


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, 2.0])
def test_concurrency_must_be_a_positive_integer(bad):
    with pytest.raises(ValueError, match="positive integer"):
        fanout.validated(concurrency=bad)


@pytest.mark.parametrize("tool", ["workflow_project_create", "workflow_project_update"])
@pytest.mark.parametrize("bad", [True, 1.5])
def test_the_tools_refuse_a_non_integer_concurrency(tool, bad):
    """Lax pydantic coercion would turn ``true`` into 1 before ``validated``
    could see it was a bool, so the tool schema has to be strict."""
    import asyncio

    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from witan import server as srv

    args = (
        {"title": "t", "description": "d"}
        if tool == "workflow_project_create"
        else {"slug": "wp-x"}
    )

    async def _call():
        async with Client(srv.mcp) as client:
            await client.call_tool(tool, {**args, "fanout_concurrency": bad})

    with pytest.raises(ToolError, match="fanout_concurrency"):
        asyncio.run(_call())


def test_an_unknown_harness_is_refused():
    with pytest.raises(ValueError, match="'cursor'"):
        fanout.validated(harnesses=["claude", "cursor"])


def test_validated_keeps_only_what_was_passed_and_clears_empty_lists():
    assert fanout.validated() == {}
    assert fanout.validated(enabled=False, harnesses=[], labels=[" gpu ", "gpu"]) == {
        "fanout_enabled": False,
        "fanout_harnesses": None,
        "fanout_labels": ["gpu"],
    }


def test_describe_renders_nothing_until_a_field_is_set():
    assert fanout.describe(_row()) is None
    line = fanout.describe(
        _row(
            fanout_enabled=True,
            fanout_concurrency=3,
            fanout_labels=["docker"],
            fanout_set_by="alice",
        )
    )
    assert line == (
        "Fan-out on, up to 3 tasks at once, harnesses any, runner labels docker "
        "(set by alice)"
    )


# ── Tools ───────────────────────────────────────────────────────────────────


@requires_omnigraph
def test_create_get_list_and_status_round_trip(server):
    proj = server.workflow_project_create(
        title="fan-out",
        description="d",
        fanout_enabled=True,
        fanout_concurrency=3,
        fanout_harnesses=["claude", "pi"],
        fanout_labels=["docker"],
    )
    plain = server.workflow_project_create(title="sequential", description="d")

    got = server.workflow_project_get(proj["slug"])
    policy = FanoutPolicy.of(got)
    assert policy == FanoutPolicy(
        enabled=True,
        concurrency=3,
        harnesses=("claude", "pi"),
        labels=("docker",),
        set_by="alice",
    )
    assert not FanoutPolicy.of(server.workflow_project_get(plain["slug"])).enabled
    assert server.workflow_project_get(plain["slug"])["fanout_set_by"] is None

    status = server.workflow_project_status(proj["slug"])["project"]
    assert FanoutPolicy.of(status) == policy

    listed = server.workflow_project_list(all_repos=True, fanout_enabled=True)
    assert [p["slug"] for p in listed] == [proj["slug"]]
    searched = server.workflow_project_list(
        all_repos=True, query="sequential", fanout_enabled=True
    )
    assert searched == []


@requires_omnigraph
def test_update_writes_only_the_fields_passed(server, monkeypatch):
    from witan import server as srv

    proj = server.workflow_project_create(
        title="fan-out",
        description="d",
        fanout_enabled=True,
        fanout_concurrency=2,
        fanout_labels=["docker"],
    )
    written = []
    real_change_many = srv.client.change_many

    def recording_change_many(steps, **kwargs):
        written.extend(name for _, name, _ in steps)
        return real_change_many(steps, **kwargs)

    monkeypatch.setattr(srv.client, "change_many", recording_change_many)
    server.workflow_project_update(proj["slug"], fanout_concurrency=5)

    assert written == ["update_workflow_project_fanout_concurrency"]
    got = FanoutPolicy.of(server.workflow_project_get(proj["slug"]))
    assert (got.enabled, got.concurrency, got.labels) == (True, 5, ("docker",))


@requires_omnigraph
def test_a_concurrent_policy_edit_cannot_turn_fan_out_back_on(server, monkeypatch):
    """One session turns fan-out off between another's read and write. The
    other only changes the cap, so it must not write ``fanout_enabled`` back
    from its stale read."""
    from witan import server as srv

    proj = server.workflow_project_create(
        title="fan-out", description="d", fanout_enabled=True
    )
    slug = proj["slug"]
    real_read = srv.client.read
    interleaved = []

    def read_then_concurrent_write(queries, name, params):
        rows = real_read(queries, name, params)
        if name == "get_workflow_project" and not interleaved:
            interleaved.append(True)
            server.workflow_project_update(slug, fanout_enabled=False)
        return rows

    monkeypatch.setattr(srv.client, "read", read_then_concurrent_write)
    server.workflow_project_update(slug, fanout_concurrency=4, title="renamed")
    monkeypatch.setattr(srv.client, "read", real_read)

    got = server.workflow_project_get(slug)
    assert interleaved
    assert got["title"] == "renamed"
    assert got["fanout_enabled"] is False
    assert got["fanout_concurrency"] == 4


@requires_omnigraph
def test_update_records_the_caller_and_empty_lists_clear(server, monkeypatch):
    from witan import server as srv

    proj = server.workflow_project_create(
        title="fan-out",
        description="d",
        fanout_enabled=True,
        fanout_harnesses=["claude"],
        fanout_labels=["gpu"],
    )
    monkeypatch.setattr(srv, "_current_author", lambda: "bob")
    got = server.workflow_project_update(
        proj["slug"], fanout_harnesses=[], fanout_labels=[]
    )
    assert got["fanout_harnesses"] is None
    assert got["fanout_labels"] is None
    assert got["fanout_enabled"] is True
    assert got["fanout_set_by"] == "bob"


@requires_omnigraph
def test_an_unrelated_edit_leaves_the_policy_intact(server):
    proj = server.workflow_project_create(
        title="fan-out", description="d", fanout_enabled=True, fanout_concurrency=2
    )
    server.workflow_project_update(proj["slug"], title="renamed", preauthorized=["x"])
    server.workflow_project_advance(proj["slug"], phase="spec")

    got = FanoutPolicy.of(server.workflow_project_get(proj["slug"]))
    assert (got.enabled, got.concurrency, got.set_by) == (True, 2, "alice")


@requires_omnigraph
def test_invalid_policy_is_refused_before_anything_is_written(server):
    proj = server.workflow_project_create(title="fan-out", description="d")
    with pytest.raises(ValueError):
        server.workflow_project_update(
            proj["slug"], title="renamed", fanout_concurrency=0
        )
    assert server.workflow_project_get(proj["slug"])["title"] == "fan-out"
    with pytest.raises(ValueError):
        server.workflow_project_create(
            title="bad", description="d", fanout_harnesses=["cursor"]
        )


# ── CLI ─────────────────────────────────────────────────────────────────────


def _patch_server(monkeypatch, srv):
    from witan.cli import _common

    monkeypatch.setattr(_common, "_server", srv)
    printed = []
    monkeypatch.setattr(
        _common.console, "print", lambda *a, **kw: printed.append(str(a[0]))
    )
    return printed


def _run_cli(argv):
    import witan.cli  # noqa: F401  (registers the subcommands)
    from witan.cli._common import app

    command, bound, _ = app.parse_args(argv)
    command(*bound.args, **bound.kwargs)


@requires_omnigraph
def test_cli_sets_shows_and_clears_the_policy(server, monkeypatch):
    from witan.cli._common import _fn

    printed = _patch_server(monkeypatch, server)
    _run_cli(
        [
            "project",
            "create",
            "cli fan-out",
            "--fanout",
            "--fanout-concurrency",
            "2",
            "--fanout-harness",
            "claude",
            "--fanout-harness",
            "pi",
            "--fanout-label",
            "docker",
        ]
    )
    slug = _fn(server.workflow_project_list)(all_repos=True)[0]["slug"]
    got = FanoutPolicy.of(_fn(server.workflow_project_get)(slug=slug))
    assert got == FanoutPolicy(
        enabled=True,
        concurrency=2,
        harnesses=("claude", "pi"),
        labels=("docker",),
        set_by="alice",
    )

    from witan.cli import projects as projects_cli

    tables = []
    monkeypatch.setattr(projects_cli, "render_table", lambda **kw: tables.append(kw))
    _fn(server.workflow_project_create)(title="sequential", description="d")
    _run_cli(["projects", "--all-repos", "--fanout"])
    assert [r["slug"] for r in tables[-1]["rows"]] == [slug]

    printed.clear()
    _run_cli(["project", "update", slug, "--no-fanout", "--empty-fanout-harness"])
    got = FanoutPolicy.of(_fn(server.workflow_project_get)(slug=slug))
    assert (got.enabled, got.harnesses, got.labels) == (False, (), ("docker",))
    assert (
        "  Fan-out off, up to 2 tasks at once, harnesses any, runner labels docker "
        "(set by alice)"
    ) in printed

    assert _fn(server.workflow_project_list)(all_repos=True, fanout_enabled=True) == []


def test_cli_refuses_an_unknown_harness():
    import cyclopts

    import witan.cli  # noqa: F401
    from witan.cli._common import app

    with pytest.raises(cyclopts.CycloptsError, match="cursor"):
        app.parse_args(
            ["project", "create", "x", "--fanout-harness", "cursor"],
            exit_on_error=False,
            print_error=False,
        )
