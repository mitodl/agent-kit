"""witan-council's language-server features, over real JSON-RPC.

The in-process harness (``witan_core.lsp.testing``) drives the host the way an
editor does. Most tests give the council plugin a recording stand-in for its
tool provider, so they check what each feature asks the graph and how it
renders the answer; the omnigraph-backed ones at the end run the commands
against a real local store.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from lsprotocol import types

from witan.lsp_plugin import (
    LIST_MY_WORK,
    LIST_PROJECTS,
    LIST_READY_TASKS,
    LIST_STALE_REFS,
    RELEASE_TASK,
    RUN_PROJECT,
    RUN_TASK,
    CouncilPlugin,
)
from witan.prompts import prompts_allowed
from witan.session_state import SESSION_ID_ENV_VARS
from witan_core.lsp import Capability, Plugin, Route, escape_markdown
from witan_core.lsp.host import Host
from witan_core.lsp.testing import lsp_session
from witan_core.remote.oidc import NeedsLogin

from .conftest import requires_omnigraph

REPO = "https://github.com/test/repo"
SYMBOL = f"{REPO}#app/svc.py::Service.run"


def _checkout(path: Path, remote: str | None = REPO) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    # `git rev-parse` names no branch before the first commit.
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.org"]
    subprocess.run(
        ["git", "-C", str(path), *identity, "commit", "-q", "--allow-empty", "-m", "i"],
        check=True,
    )
    if remote:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", remote], check=True
        )
    return path


class FakeTools:
    """The council tools, recording each call and whether it could prompt."""

    def __init__(self, **results):
        self.results = results
        self.calls: list[tuple[str, dict]] = []
        self.prompts_allowed: list[bool] = []

    def __getattr__(self, name):
        if name not in CouncilPlugin.tools:
            raise AttributeError(name)

        def call(**kwargs):
            self.calls.append((name, kwargs))
            self.prompts_allowed.append(prompts_allowed())
            result = self.results.get(name)
            if isinstance(result, Exception):
                raise result
            return result(**kwargs) if callable(result) else result

        return call

    def called(self, name):
        return [kwargs for n, kwargs in self.calls if n == name]


class Council(CouncilPlugin):
    """The real plugin with its graph swapped for ``provider``."""

    def __init__(self, provider):
        super().__init__()
        self.provider = provider

    def route(self, folder):
        return Route(key="fake", local=False)

    def open(self, route, folder):
        return self.provider


class Symbols(Plugin):
    """Stands in for witan-code's symbol resolution: line 3 is ``SYMBOL``."""

    capabilities = frozenset({Capability.SYMBOL_RESOLUTION})

    def route(self, folder):
        return Route(key="symbols", local=False)

    def open(self, route, folder):
        return object()

    def resolve_symbol(self, ctx, uri, line, character):
        return SYMBOL if line == 3 else None


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def checkout(tmp_path):
    root = _checkout(tmp_path / "repo")
    (root / "app").mkdir()
    (root / "app" / "svc.py").write_text("class Service:\n    def run(self): ...\n")
    return root


async def _hover(session, path: Path, line: int):
    return await session.client.text_document_hover_async(
        types.HoverParams(
            text_document=types.TextDocumentIdentifier(uri=path.as_uri()),
            position=types.Position(line=line, character=4),
        )
    )


# ── hover ─────────────────────────────────────────────────────────────────


def _recall(**kwargs):
    return {
        "memories": [
            {
                "slug": "les-a",
                "kind": "lesson",
                "title": "Retries [click](https://evil.example) ![x](y) <b>hi</b>",
            }
        ],
        "symbol": {
            "symbol_id": kwargs["symbol_id"],
            "tasks": [
                {
                    "slug": "tk-a",
                    "status": "open",
                    "title": "# Heading\n* not a list `code`",
                }
            ],
        },
    }


def test_hover_lists_tasks_and_memories_naming_the_symbol(checkout):
    tools = FakeTools(recall=_recall)
    host = Host({"code": Symbols(), "council": Council(tools)})

    async def go():
        async with lsp_session(host, [checkout]) as session:
            hover = await _hover(session, checkout / "app" / "svc.py", 3)
            nothing = await _hover(session, checkout / "app" / "svc.py", 1)
            return hover, nothing

    hover, nothing = run(go())
    text = hover.contents.value
    assert hover.contents.kind == types.MarkupKind.Markdown
    assert "Tasks:" in text and "Memories:" in text
    assert escape_markdown("# Heading\n* not a list `code`") in text
    assert (
        escape_markdown("Retries [click](https://evil.example) ![x](y) <b>hi</b>")
        in text
    )
    # Teammate text never forms a link, an image or HTML.
    assert "](" not in text.replace("\\](", "")
    assert "<b>" not in text
    assert nothing is None
    assert tools.called("recall") == [
        {"symbol_id": SYMBOL, "repo": REPO, "hops": 0, "limit": 10}
    ]


def test_hover_is_off_without_symbol_resolution(checkout):
    tools = FakeTools(recall=_recall)

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [checkout]) as s:
            return await _hover(s, checkout / "app" / "svc.py", 3)

    assert run(go()) is None
    assert tools.calls == []


def test_hover_with_nothing_to_show_is_empty(checkout):
    tools = FakeTools(recall={"memories": [], "symbol": {"tasks": []}})
    host = Host({"code": Symbols(), "council": Council(tools)})

    async def go():
        async with lsp_session(host, [checkout]) as s:
            return await _hover(s, checkout / "app" / "svc.py", 3)

    assert run(go()) is None


# ── stale-ref diagnostics ─────────────────────────────────────────────────


def _stale(**kwargs):
    return {
        "repo": kwargs["repo"],
        "checked": 3,
        "memories": [
            {
                "slug": "pat-moved",
                "kind": "pattern",
                "title": "How runs retry",
                "status": "stale",
                "refs": [
                    {
                        "symbol_ref": f"{REPO}#app/svc.py::Service.run",
                        "status": "missing",
                        "candidates": [f"{REPO}#app/runner.py::Service.run"],
                    },
                    {"symbol_ref": f"{REPO}#app/svc.py::Service", "status": "live"},
                    {
                        "symbol_ref": f"{REPO}#app/gone.py::old",
                        "status": "missing",
                        "candidates": [],
                    },
                    {
                        # A teammate-written path out of the checkout.
                        "symbol_ref": f"{REPO}#../outside.py::escape",
                        "status": "missing",
                        "candidates": [],
                    },
                ],
            },
            {
                "slug": "les-elsewhere",
                "kind": "lesson",
                "title": "Other repo",
                "status": "unknown",
                "refs": [
                    {
                        "symbol_ref": "https://github.com/test/other#app/svc.py::x",
                        "status": "unknown",
                        "reason": "not indexed",
                    }
                ],
            },
        ],
    }


def test_a_stale_ref_is_a_note_on_the_first_line_of_its_file(checkout):
    tools = FakeTools(memory_stale_refs=_stale)
    (checkout / "app" / "other.py").write_text("")

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [checkout]) as s:
            named = await s.open(checkout / "app" / "svc.py")
            other = await s.open(checkout / "app" / "other.py")
            return named, other

    named, other = run(go())
    assert len(named) == 1
    note = named[0]
    assert note.severity == types.DiagnosticSeverity.Information
    assert note.range.start.line == 0
    assert "pat-moved" in note.message and "How runs retry" in note.message
    assert f"{REPO}#app/runner.py::Service.run" in note.message
    assert other == []
    # Two opens, one review: the result is reused within its TTL.
    assert tools.called("memory_stale_refs") == [{"repo": REPO}]
    assert tools.prompts_allowed == [False]


def test_a_logged_out_deployment_says_so_on_the_file(checkout):
    tools = FakeTools(
        memory_stale_refs=NeedsLogin("Not logged in to https://w.example/mcp")
    )

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [checkout]) as s:
            return await s.open(checkout / "app" / "svc.py")

    [note] = run(go())
    assert note.message == (
        "Could not check stale memory refs: Not logged in to https://w.example/mcp"
    )


def test_list_stale_refs_returns_every_ref_not_live(checkout):
    tools = FakeTools(memory_stale_refs=_stale)
    (checkout.parent / "outside.py").write_text("")

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [checkout]) as s:
            return await s.command(LIST_STALE_REFS, {"uri": checkout.as_uri()})

    result = run(go())
    rows = {r["symbol_ref"].rpartition("::")[2]: r for r in result["refs"]}
    assert result["repo"] == REPO
    assert set(rows) == {"Service.run", "old", "escape", "x"}
    assert rows["Service.run"]["uri"] == (checkout / "app" / "svc.py").as_uri()
    # Its file is gone, so this command is the only place it shows.
    assert rows["old"]["uri"] is None
    # It exists, but outside the checkout, so the editor is not pointed at it.
    assert rows["escape"]["uri"] is None
    assert rows["x"]["uri"] is None and rows["x"]["status"] == "unknown"


def test_a_folder_opened_through_a_symlink_gets_its_notes(checkout, tmp_path):
    """git names the physical checkout; the editor names the link."""
    link = tmp_path / "link"
    link.symlink_to(checkout)
    tools = FakeTools(memory_stale_refs=_stale)

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [link]) as s:
            notes = await s.open(link / "app" / "svc.py")
            refs = await s.command(LIST_STALE_REFS)
            return notes, refs

    notes, refs = run(go())
    assert len(notes) == 1 and "pat-moved" in notes[0].message
    [row] = [r for r in refs["refs"] if r["uri"]]
    assert row["uri"] == (link / "app" / "svc.py").as_uri()


def test_a_malformed_missing_ref_costs_no_other_note(checkout):
    """Another deployment's server may call a ref missing that this one
    cannot split; it is skipped, not a TypeError that drops the file."""

    def stale(**kwargs):
        result = _stale(**kwargs)
        result["memories"][0]["refs"].insert(
            0, {"symbol_ref": "not-a-symbol-id", "status": "missing"}
        )
        return result

    tools = FakeTools(memory_stale_refs=stale)

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [checkout]) as s:
            return await s.open(checkout / "app" / "svc.py")

    [note] = run(go())
    assert "pat-moved" in note.message


# ── list commands ─────────────────────────────────────────────────────────


def test_list_ready_tasks_reads_the_branch_checked_out_now(checkout):
    tools = FakeTools(task_ready=[], task_for_branch=[])

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [checkout]) as s:
            await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(checkout), "switch", "-q", "-c", "later"],
                check=True,
            )
            return await s.command(LIST_READY_TASKS)

    assert run(go())["branch"] == "later"
    assert tools.called("task_for_branch") == [{"branch": "later", "repo": REPO}]


def test_a_uri_spelled_through_a_symlink_names_its_folder(checkout, tmp_path):
    """The folder was opened by its physical path; the client names a file
    through a link to it."""
    link = tmp_path / "link"
    link.symlink_to(checkout)
    tools = FakeTools(task_list=[])

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [checkout]) as s:
            return await s.command(
                LIST_MY_WORK, {"uri": (link / "app" / "svc.py").as_uri()}
            )

    assert run(go()) == {"repo": REPO, "tasks": []}


def test_list_commands_scope_every_call_to_the_folders_repo(checkout, tmp_path):
    other = _checkout(tmp_path / "other", "https://github.com/test/other")
    subprocess.run(
        ["git", "-C", str(other), "checkout", "-q", "-b", "feature"], check=True
    )
    tools = FakeTools(
        task_ready=[{"slug": "tk-r"}],
        task_for_branch=[{"slug": "tk-b"}],
        workflow_project_list=[{"slug": "wp-p"}],
        task_list=[{"slug": "tk-mine"}],
    )

    async def go():
        async with lsp_session(
            Host({"council": Council(tools)}), [checkout, other]
        ) as s:
            file_in_other = (other / "x.py").as_uri()
            return (
                await s.command(LIST_READY_TASKS, {"uri": file_in_other}),
                await s.command(LIST_PROJECTS),
                await s.command(LIST_MY_WORK, {"uri": checkout.as_uri()}),
            )

    ready, projects, mine = run(go())
    assert ready["ready"][0]["slug"] == "tk-r"
    assert ready["for_branch"][0]["slug"] == "tk-b"
    assert ready["repo"] == "https://github.com/test/other"
    assert projects == {"repo": REPO, "projects": [{"slug": "wp-p"}]}
    assert mine == {"repo": REPO, "tasks": [{"slug": "tk-mine"}]}
    assert tools.calls == [
        ("task_ready", {"repo": "https://github.com/test/other"}),
        (
            "task_for_branch",
            {"branch": "feature", "repo": "https://github.com/test/other"},
        ),
        ("workflow_project_list", {"repo": REPO}),
        ("task_list", {"status": "in_progress", "assignee": "@me", "repo": REPO}),
    ]
    assert not any(tools.prompts_allowed)


def test_a_folder_with_no_remote_is_refused_with_a_reason(tmp_path):
    bare = _checkout(tmp_path / "bare", remote=None)
    tools = FakeTools()

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [bare]) as s:
            with pytest.raises(Exception, match="has no git remote"):
                await s.command(LIST_READY_TASKS)

    run(go())
    assert tools.calls == []


# ── launch and release ────────────────────────────────────────────────────

_CLAIM = {
    "slug": "tk-a",
    "claimed": True,
    "assignee": "me@example.edu",
    "claimed_at": "2026-11-17T16:20:04.607+00:00",
    "overlaps": [],
    "memories": [],
}


def test_run_task_returns_the_launch_spec_and_spawns_nothing(checkout, monkeypatch):
    for name in SESSION_ID_ENV_VARS:
        monkeypatch.setenv(name, "outer-session")
    for name in ("WITAN_TARGET", "CLAUDE_PROJECT_DIR", "WITAN_REPO"):
        monkeypatch.delenv(name, raising=False)
    seen_env = []

    def claim(**kwargs):
        seen_env.append({n: n in os.environ for n in SESSION_ID_ENV_VARS})
        return dict(_CLAIM)

    tools = FakeTools(
        task_get={"slug": "tk-a", "title": "T", "type": "bug", "repo": REPO},
        task_claim=claim,
    )

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [checkout]) as s:
            return await s.command(RUN_TASK, {"slug": "tk-a", "agent": "pi"})

    spec = run(go())
    assert spec["launchable"] is True
    assert spec["kind"] == "task"
    assert spec["cwd"] == str(checkout)
    assert spec["agent"] == "pi"
    # One folder and no overrides, but the spec claimed, and a handoff never
    # holds a claim.
    assert spec["handoff"] is False
    assert spec["claim"]["claimed_at"] == _CLAIM["claimed_at"]
    # The release goes back through the folder the launch was invoked from.
    assert spec["release"] == {
        "slug": "tk-a",
        "claimed_at": _CLAIM["claimed_at"],
        "uri": checkout.as_uri(),
    }
    # The claim is bare, and prompts are off for every call the launch makes.
    assert seen_env == [dict.fromkeys(SESSION_ID_ENV_VARS, False)]
    assert not any(tools.prompts_allowed)
    assert tools.called("task_claim") == [
        {"slug": "tk-a", "force": False, "repo": REPO, "branch": "main"}
    ]


def _unclaimed(checkout, arguments):
    tools = FakeTools(
        task_get={"slug": "tk-a", "title": "T", "type": "bug", "repo": REPO},
        recall={"memories": []},
    )

    async def go():
        host = Host({"council": Council(tools)})
        async with lsp_session(host, [checkout]) as s:
            return await s.command(RUN_TASK, arguments)

    return run(go()), tools


def test_run_task_without_a_claim_claims_nothing(checkout, monkeypatch):
    """The spec a chat-panel handoff needs: its prompt has the agent claim."""
    for name in ("WITAN_TARGET", "CLAUDE_PROJECT_DIR", "WITAN_REPO"):
        monkeypatch.delenv(name, raising=False)

    spec, tools = _unclaimed(checkout, {"slug": "tk-a", "claim": False})

    assert spec["launchable"] is True and spec["claim"] is None
    assert 'task_claim(slug="tk-a")' in spec["prompt"]
    assert "release" not in spec
    assert tools.called("task_claim") == []
    # Still never a handoff: this process cannot see the editor's overrides.
    assert spec["handoff"] is False


def test_run_task_refuses_a_claim_that_is_not_a_boolean(checkout):
    with pytest.raises(Exception, match="true or false"):
        _unclaimed(checkout, {"slug": "tk-a", "claim": "no"})


def test_release_names_the_invoking_folder_when_the_launch_runs_in_another(
    checkout, tmp_path
):
    """The claim went through the invoking folder's route, and `cwd` is the
    other open folder that is a checkout of the task's repo."""
    invoking = _checkout(tmp_path / "other", "https://github.com/test/other")
    tools = FakeTools(
        task_get={"slug": "tk-a", "title": "T", "type": "bug", "repo": REPO},
        task_claim=dict(_CLAIM),
    )

    async def go():
        host = Host({"council": Council(tools)})
        async with lsp_session(host, [invoking, checkout]) as s:
            return await s.command(RUN_TASK, {"slug": "tk-a", "uri": invoking.as_uri()})

    spec = run(go())
    assert spec["cwd"] == str(checkout)
    assert spec["release"]["uri"] == invoking.as_uri()


def test_run_project_claims_nothing(checkout):
    tools = FakeTools(
        workflow_project_get={"slug": "wp-p", "title": "P", "repos": [REPO]},
        task_ready=[],
    )

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [checkout]) as s:
            return await s.command(RUN_PROJECT, {"slug": "wp-p"})

    spec = run(go())
    assert spec["launchable"] is True and spec["claim"] is None
    assert tools.called("task_claim") == []


def test_release_is_fenced_on_the_specs_claim_and_never_forced(checkout):
    tools = FakeTools(task_release={"slug": "tk-a", "released": True})

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [checkout]) as s:
            released = await s.command(
                RELEASE_TASK,
                {
                    "slug": "tk-a",
                    "claimed_at": _CLAIM["claimed_at"],
                    "uri": checkout.as_uri(),
                },
            )
            with pytest.raises(Exception, match="claimed_at"):
                await s.command(
                    RELEASE_TASK, {"slug": "tk-a", "uri": checkout.as_uri()}
                )
            # No first-folder default: the client has to name the launch's.
            with pytest.raises(Exception, match="'uri'"):
                await s.command(
                    RELEASE_TASK, {"slug": "tk-a", "claimed_at": _CLAIM["claimed_at"]}
                )
            return released

    assert run(go())["released"] is True
    assert tools.called("task_release") == [
        {"slug": "tk-a", "if_claimed_at": _CLAIM["claimed_at"]}
    ]


def test_a_logged_out_command_fails_with_the_login_hint(checkout):
    tools = FakeTools(task_ready=NeedsLogin("Not logged in, run `witan login` first."))

    async def go():
        async with lsp_session(Host({"council": Council(tools)}), [checkout]) as s:
            with pytest.raises(Exception, match="witan login"):
                await s.command(LIST_READY_TASKS)

    run(go())


class GuardedCouncil(CouncilPlugin):
    """The real plugin and route, with ``tools`` behind the local-store guard."""

    def __init__(self, tools):
        super().__init__()
        self.tools_ = tools

    def open(self, route, folder):
        from witan.cli.local_dispatch import local_server

        return local_server(route.guard, load_inner=lambda: self.tools_)


def test_a_guarded_local_folder_reads_and_refuses_writes_with_a_reason(
    checkout, tmp_path, monkeypatch
):
    """A deployment is configured and this folder matches nothing, so its
    reads fall back to the local store and its writes are refused, saying why
    rather than as an exit code."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[targets.work]\n"
        'remote_url = "https://witan.example.org/mcp"\n'
        'oidc_issuer = "https://sso.example.org/realms/ol"\n'
        'match_repos = ["https://github.com/work/elsewhere"]\n'
    )
    monkeypatch.setenv("WITAN_CONFIG", str(cfg))
    # Naming a store outright makes the fallback deliberate, and unguarded.
    monkeypatch.delenv("WITAN_MEMORY_URI", raising=False)
    tools = FakeTools(
        task_ready=[],
        task_for_branch=[],
        task_get={"slug": "tk-a", "title": "T", "type": "bug", "repo": REPO},
    )

    async def go():
        async with lsp_session(
            Host({"council": GuardedCouncil(tools)}), [checkout]
        ) as s:
            ready = await s.command(LIST_READY_TASKS)
            with pytest.raises(Exception, match="refusing to run `task_claim`"):
                await s.command(RUN_TASK, {"slug": "tk-a"})
            return ready

    assert run(go())["ready"] == []
    assert [name for name, _ in tools.calls] == [
        "task_ready",
        "task_for_branch",
        "task_get",
    ]


# ── against a real local store ────────────────────────────────────────────


class LocalCouncil(CouncilPlugin):
    """The real plugin and route, opening the test's in-process store."""

    def open(self, route, folder):
        from witan import server as srv

        return srv


@requires_omnigraph
def test_launch_list_and_release_against_a_local_store(server, checkout, monkeypatch):
    monkeypatch.chdir(checkout)
    task = server.task_create(title="Fix retries", description="x", repo=REPO)

    async def go():
        async with lsp_session(Host({"council": LocalCouncil()}), [checkout]) as s:
            ready = await s.command(LIST_READY_TASKS)
            spec = await s.command(RUN_TASK, {"slug": task["slug"]})
            mine = await s.command(LIST_MY_WORK)
            released = await s.command(RELEASE_TASK, spec["release"])
            again = await s.command(RELEASE_TASK, spec["release"])
            return ready, spec, mine, released, again

    ready, spec, mine, released, again = run(go())
    assert task["slug"] in [t["slug"] for t in ready["ready"]]
    assert spec["launchable"] is True and spec["claim"]["claimed"] is True
    assert [t["slug"] for t in mine["tasks"]] == [task["slug"]]
    assert released["released"] is True
    assert again["released"] is False and again["reason"] == "claim_changed"
    assert server.task_get(task["slug"])["status"] == "open"
