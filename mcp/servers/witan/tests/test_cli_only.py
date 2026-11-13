"""Admin and corpus-mining tools are listed to witan's CLI and to nobody else.

``witan serve`` over stdio and the deployment over streamable HTTP serve the same
``witan.server.mcp`` instance, and the filter is middleware on that instance, so
an in-memory client exercises the same ``tools/list`` path both transports do.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastmcp import Client
from mcp.types import Implementation

from witan.cli_only import CLI_ONLY_TOOLS
from witan_core.remote.proxy import proxy_client_info

from .conftest import requires_omnigraph
from .test_cli import _patch_server


def _listed(client_info: Implementation | None = None) -> set[str]:
    import witan.server as srv

    async def _list() -> set[str]:
        async with Client(srv.mcp, client_info=client_info) as client:
            return {t.name for t in await client.list_tools()}

    return asyncio.run(_list())


def test_cli_only_tools_are_absent_from_an_agents_tools_list(server):
    listed = _listed(Implementation(name="claude-code", version="2.1.0"))
    assert listed, "expected the server to list some tools"
    assert not (CLI_ONLY_TOOLS & listed)


def test_cli_only_tools_are_absent_for_a_client_that_names_no_one(server):
    assert not (CLI_ONLY_TOOLS & _listed())


def test_the_cli_proxy_is_listed_every_cli_only_tool(server):
    # Also what keeps CLI_ONLY_TOOLS from naming a tool that no longer exists.
    assert CLI_ONLY_TOOLS <= _listed(proxy_client_info())


@requires_omnigraph
def test_an_unlisted_tool_is_still_callable_by_name(server):
    """The filter is discoverability, not access control: ``tools/call`` is
    untouched, which is also what an agent with a stale tool list relies on."""
    import witan.server as srv

    async def _call():
        async with Client(srv.mcp) as client:
            return await client.call_tool("workflow_trace_list", {"all_repos": True})

    assert asyncio.run(_call()).is_error is False


@requires_omnigraph
def test_trace_annotate_cli_records_slugs(server, monkeypatch):
    from witan.cli.traces import trace_annotate_cmd

    proj = server.workflow_project_create(title="annotate via cli", description="d")
    server.workflow_project_complete(proj["slug"], outcome="done")

    printed = _patch_server(monkeypatch, server)
    trace_annotate_cmd(proj["slug"], lesson=["les-a"], pattern=["pat-a", "pat-b"])

    assert "1 lesson(s), 2 pattern(s)" in "\n".join(printed)
    trace = server.workflow_trace_get(proj["slug"])
    assert trace["lessons_slug"] == ["les-a"]
    assert trace["patterns_slug"] == ["pat-a", "pat-b"]


@requires_omnigraph
def test_trace_mine_cli_prints_material_then_creates_memories(
    server, monkeypatch, tmp_path
):
    from witan.cli.traces import trace_mine_cmd

    proj = server.workflow_project_create(title="mine via cli", description="d")
    sess = server.workflow_session_start(
        project_slug=proj["slug"], session_id=uuid.uuid4().hex, phase="discovery"
    )
    server.workflow_session_end(sess["session_slug"], summary="learned a thing")
    done = server.workflow_project_complete(proj["slug"], outcome="shipped it")

    printed = _patch_server(monkeypatch, server)
    trace_mine_cmd(done["trace_slug"])
    material = "\n".join(printed)
    assert "shipped it" in material
    assert "learned a thing" in material

    proposals = tmp_path / "proposals.json"
    proposals.write_text(
        json.dumps(
            {
                "patterns": [{"title": "a pattern", "content": "do X because Y"}],
                "lessons": [{"title": "a lesson", "content": "watch out for Z"}],
            }
        )
    )
    printed.clear()
    trace_mine_cmd(proj["slug"], proposals=proposals)

    trace = server.workflow_trace_get(done["trace_slug"])
    assert len(trace["patterns_slug"]) == 1
    assert len(trace["lessons_slug"]) == 1
    created = "\n".join(printed)
    assert trace["patterns_slug"][0] in created
    assert trace["lessons_slug"][0] in created


def _fake_trace_server(monkeypatch, result: dict) -> tuple[list[dict], list[str]]:
    """Point the CLI at a stub whose ``workflow_trace_mine`` records its kwargs."""
    calls: list[dict] = []

    def workflow_trace_mine(**kwargs):
        calls.append(kwargs)
        return result

    printed = _patch_server(
        monkeypatch, SimpleNamespace(workflow_trace_mine=workflow_trace_mine)
    )
    return calls, printed


@pytest.mark.parametrize(
    "spec",
    [
        {"pattern": [{"title": "t", "content": "c"}]},
        {"patterns": [], "lessons": []},
        [{"title": "t", "content": "c"}],
    ],
    ids=["typo-key", "empty-lists", "top-level-list"],
)
def test_trace_mine_refuses_proposals_that_propose_nothing(spec, monkeypatch, tmp_path):
    from witan.cli.traces import trace_mine_cmd

    calls, printed = _fake_trace_server(monkeypatch, {})
    proposals = tmp_path / "proposals.json"
    proposals.write_text(json.dumps(spec))

    with pytest.raises(SystemExit) as exc:
        trace_mine_cmd("wt-x", proposals=proposals)

    assert exc.value.code == 1
    assert calls == [], "an empty proposal set must not reach the write path"
    assert "proposals.json" in "\n".join(printed)


def test_trace_mine_fills_the_callers_repo_into_proposals(monkeypatch, tmp_path):
    """A deployment has no checkout to detect a repo from, and the proxy only
    fills a tool's top-level ``repo``, never one nested in a proposal."""
    from witan.cli.traces import trace_mine_cmd

    monkeypatch.setenv("WITAN_REPO", "https://github.com/acme/widgets")
    calls, _ = _fake_trace_server(
        monkeypatch, {"created_patterns": [], "created_lessons": []}
    )
    proposals = tmp_path / "proposals.json"
    proposals.write_text(
        json.dumps(
            {
                "patterns": [{"title": "p", "content": "c"}],
                "lessons": [
                    {"title": "l", "content": "c", "repo": None},
                    {"title": "l2", "content": "c", "repo": "https://github.com/a/b"},
                ],
            }
        )
    )

    trace_mine_cmd("wt-x", proposals=proposals)

    (call,) = calls
    assert [p["repo"] for p in call["patterns"]] == ["https://github.com/acme/widgets"]
    assert [ls["repo"] for ls in call["lessons"]] == [
        "https://github.com/acme/widgets",
        "https://github.com/a/b",
    ]


def test_trace_mine_leaves_an_all_repos_proposal_unscoped(monkeypatch, tmp_path):
    from witan.cli.traces import trace_mine_cmd

    monkeypatch.setenv("WITAN_REPO", "https://github.com/acme/widgets")
    calls, _ = _fake_trace_server(
        monkeypatch, {"created_patterns": [], "created_lessons": []}
    )
    proposals = tmp_path / "proposals.json"
    proposals.write_text(
        json.dumps({"lessons": [{"title": "l", "content": "c", "all_repos": True}]})
    )

    trace_mine_cmd("wt-x", proposals=proposals)

    assert "repo" not in calls[0]["lessons"][0]


def test_trace_mine_leaves_repo_unset_when_none_is_detected(monkeypatch, tmp_path):
    from witan.cli.traces import trace_mine_cmd

    monkeypatch.setenv("WITAN_REPO", "")
    calls, _ = _fake_trace_server(
        monkeypatch, {"created_patterns": [], "created_lessons": []}
    )
    proposals = tmp_path / "proposals.json"
    proposals.write_text(json.dumps({"patterns": [{"title": "p", "content": "c"}]}))

    trace_mine_cmd("wt-x", proposals=proposals)

    assert "repo" not in calls[0]["patterns"][0]


def test_trace_mine_prints_the_servers_redaction_note(monkeypatch, tmp_path):
    from witan.cli.traces import trace_mine_cmd

    note = "⚠ CONTENT WAS ALTERED BEFORE STORAGE: pat-p.content[0:16] matched x."
    _, printed = _fake_trace_server(
        monkeypatch,
        {"created_patterns": ["pat-p"], "created_lessons": [], "redaction_note": note},
    )
    proposals = tmp_path / "proposals.json"
    proposals.write_text(json.dumps({"patterns": [{"title": "p", "content": "c"}]}))

    trace_mine_cmd("wt-x", proposals=proposals)

    assert note in "\n".join(printed)


def test_trace_annotate_honours_output_format(monkeypatch, capsys):
    from witan.cli.output import set_output_format
    from witan.cli.traces import trace_annotate_cmd

    record = {"slug": "wt-x", "lessons_slug": ["les-a"], "patterns_slug": []}
    _patch_server(
        monkeypatch, SimpleNamespace(workflow_trace_annotate=lambda **_: record)
    )
    set_output_format("json")

    trace_annotate_cmd("wt-x", lesson=["les-a"])

    assert json.loads(capsys.readouterr().out) == record
