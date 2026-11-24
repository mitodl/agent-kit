"""The web UI's reads, relayed by the language server for an editor webview.

The host owns the relay (``witan.uiRead``, ``witan.uiTools``) and refuses any
tool outside ADR 0011's bound set; these tests hold witan-council's side of
it: the plugin declares its share of that set, ``witan.uiBundle`` names the
bundle this install ships, and every council read the page makes comes back
over JSON-RPC from a real local store.
"""

from __future__ import annotations

import asyncio

import pytest

from witan import ui_access, ui_routes
from witan.lsp_plugin import UI_BUNDLE, CouncilPlugin
from witan_core.lsp import UI_READ_TOOLS
from witan_core.lsp.host import UI_READ_COMMAND, UI_TOOLS_COMMAND, Host
from witan_core.lsp.testing import lsp_session

from .conftest import requires_omnigraph
from .test_lsp_council_features import (
    REPO,
    Council,
    FakeTools,
    LocalCouncil,
    _checkout,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def checkout(tmp_path):
    return _checkout(tmp_path / "repo")


def test_the_hosts_ui_read_set_is_adr_0011s():
    assert UI_READ_TOOLS == ui_access.READ_TOOLS


def test_the_council_plugin_declares_every_council_read_the_page_makes():
    council_reads = ui_access.READ_TOOLS - ui_access.OPTIONAL_READ_TOOLS
    assert CouncilPlugin.tools & UI_READ_TOOLS == council_reads


def test_ui_bundle_names_the_installed_bundle(checkout, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<!doctype html>")
    monkeypatch.setattr(ui_routes, "BUNDLE_DIR", tmp_path)

    async def go():
        async with lsp_session(
            Host({"council": Council(FakeTools())}), [checkout]
        ) as s:
            return await s.command(UI_BUNDLE)

    assert run(go()) == {"path": str(tmp_path)}


def test_a_missing_bundle_says_how_to_build_it(checkout, tmp_path, monkeypatch):
    monkeypatch.setattr(ui_routes, "BUNDLE_DIR", tmp_path)

    async def go():
        async with lsp_session(
            Host({"council": Council(FakeTools())}), [checkout]
        ) as s:
            await s.command(UI_BUNDLE)

    with pytest.raises(Exception, match="npm run build"):
        run(go())


@requires_omnigraph
def test_every_council_read_the_page_makes_crosses_json_rpc(
    server, checkout, monkeypatch
):
    """Each read with the arguments the page sends, compared with the same
    call in-process: what the relay answers is the tool's own value, and all
    of it survives JSON-RPC."""
    monkeypatch.chdir(checkout)
    task = server.task_create(title="Fix retries", description="x", repo=REPO)
    memory = server.memory_store(
        kind="pattern", title="Retry with backoff", content="x", repo=REPO
    )
    project = server.workflow_project_create(
        title="Retries", description="d", repos=[REPO]
    )
    reads = {
        "task_ready": {"all_repos": True},
        "task_get": {"slug": task["slug"]},
        "task_list": {"repo": REPO, "status": "open"},
        "task_search": {"query": "retries", "all_repos": True},
        "workflow_project_get": {"slug": project["slug"]},
        "workflow_project_status": {"slug": project["slug"]},
        "workflow_project_list": {"all_repos": True},
        "workflow_session_list": {"project_slug": project["slug"]},
        "recall": {"query": "retry", "repo": REPO},
        "memory_get": {"slug": memory["slug"], "include_topics": True},
        "memory_list": {"all_repos": True},
        "memory_search": {"query": "backoff", "all_repos": True},
        "memory_contradictions": {"all_repos": True},
        "memory_stale_refs": {"repo": REPO},
    }
    assert set(reads) == ui_access.READ_TOOLS - ui_access.OPTIONAL_READ_TOOLS
    uri = checkout.as_uri()

    async def go():
        async with lsp_session(Host({"council": LocalCouncil()}), [checkout]) as s:
            listed = await s.command(UI_TOOLS_COMMAND, {"uri": uri})
            answers = {
                tool: await s.command(
                    UI_READ_COMMAND, {"uri": uri, "tool": tool, "arguments": args}
                )
                for tool, args in reads.items()
            }
            return listed, answers

    listed, answers = run(go())
    assert listed["repo"] == REPO
    assert set(listed["tools"]) == set(reads)
    assert "ctx" not in listed["tools"]["memory_get"]
    assert "include_topics" in listed["tools"]["memory_get"]
    assert answers["task_get"]["slug"] == task["slug"]
    assert [m["slug"] for m in answers["memory_list"]] == [memory["slug"]]
    assert answers["workflow_project_status"]["project"]["slug"] == project["slug"]


def test_a_relayed_read_cannot_prompt(checkout):
    """stdin is the JSON-RPC channel, so a relayed read runs with prompts off,
    as every other council call the editor makes does."""
    tools = FakeTools(task_get={"slug": "x"})

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [checkout]) as s:
            return await s.command(
                UI_READ_COMMAND,
                {
                    "uri": checkout.as_uri(),
                    "tool": "task_get",
                    "arguments": {"slug": "x"},
                },
            )

    assert run(go()) == {"slug": "x"}
    assert tools.prompts_allowed == [False]
