"""witan's binding of the write journal: which task writes queue, and replay conflicts.

The generic mechanism (queue on a connect failure, replay in order, the lock)
is tested in witan-core. What lives here needs witan's real task tools: a
queued write that has gone stale by the time it replays, because someone else
claimed the task or it was closed since, must arrive as a comment rather than
overwrite them.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastmcp import Client

from witan.config import RemoteConfig
from witan.remote.proxy import RemoteServerProxy

from .conftest import requires_omnigraph

# Its own URL, so a proxy another test left running in a worker thread (they
# share "http://unused/mcp") cannot replay this file's journals mid-test.
URL = "http://journal-tests.invalid/mcp"
HOLDER = "ada@example.org#0000abcd"
REMOTE_CFG = RemoteConfig(url=URL, oidc_issuer="https://sso")


@pytest.fixture(autouse=True)
def _data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


@pytest.fixture
def proxy(server, monkeypatch):
    import witan.server as srv

    p = RemoteServerProxy(REMOTE_CFG, lambda: "tok")
    monkeypatch.setattr(p, "_new_client", lambda _token: Client(srv.mcp))
    return p


def _queue(proxy, tool, args, holder=HOLDER):
    entry, _ = proxy.journal.append(tool, args, context=None, holder=holder)
    return entry


def _new_task(proxy, title="replay target"):
    return proxy.task_create(title=title, description="d")["slug"]


@pytest.mark.parametrize(
    ("tool", "not_sent", "dispatched"),
    [
        ("task_comment", True, False),
        ("task_create", True, False),
        ("task_update", True, True),
        ("task_close", True, True),
        ("task_release", True, True),
        ("task_claim", False, False),
        ("task_get", False, False),
        ("memory_store", False, False),
    ],
)
def test_which_writes_queue(tool, not_sent, dispatched):
    p = RemoteServerProxy(REMOTE_CFG, str)
    assert p._buffers(tool, dispatched=False) is not_sent
    assert p._buffers(tool, dispatched=True) is dispatched


def test_the_holder_is_the_session_qualified_claim_holder(monkeypatch):
    from witan import readiness

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-1")
    p = RemoteServerProxy(REMOTE_CFG, str)
    body = base64.urlsafe_b64encode(
        json.dumps({"preferred_username": "ada@example.org"}).encode()
    ).decode()
    holder = p._journal_holder(f"h.{body}.s")
    assert holder == f"ada@example.org#{readiness.session_suffix('sess-1')}"


@requires_omnigraph
def test_a_queued_close_applies_when_the_task_is_still_ours(proxy):
    slug = _new_task(proxy)
    proxy.task_claim(slug=slug, assignee=HOLDER)
    _queue(proxy, "task_close", {"slug": slug, "resolution": "done"})

    task = proxy.task_get(slug=slug)

    assert task["status"] == "closed"
    assert not proxy.journal.has_pending()


@requires_omnigraph
def test_a_queued_close_applies_when_another_of_our_sessions_holds_it(proxy):
    # Compared as people, the way task_release compares: a close queued from a
    # plain shell (or another session) on your own claim is still yours.
    slug = _new_task(proxy)
    proxy.task_claim(slug=slug, assignee="ada@example.org#99999999")
    _queue(proxy, "task_close", {"slug": slug}, holder="ada@example.org")

    assert proxy.task_get(slug=slug)["status"] == "closed"


@requires_omnigraph
def test_a_queued_close_on_a_task_someone_else_holds_becomes_a_comment(proxy):
    slug = _new_task(proxy)
    proxy.task_claim(slug=slug, assignee="bob@example.org#11112222")
    _queue(proxy, "task_close", {"slug": slug, "resolution": "done"})

    task = proxy.task_get(slug=slug)

    assert task["status"] == "in_progress"
    [comment] = task["comments"]
    assert "buffered task_close" in comment["body"]
    assert f"by {HOLDER} not applied" in comment["body"]
    assert "held by bob@example.org#11112222" in comment["body"]
    assert '"resolution": "done"' in comment["body"]


@requires_omnigraph
def test_a_queued_update_on_a_task_closed_since_becomes_a_comment(proxy, server):
    slug = server.task_create(title="closed since", description="d")["slug"]
    _queue(proxy, "task_update", {"slug": slug, "priority": "p0"})
    # In-process, so this close does not replay the entry first.
    server.task_close(slug=slug)

    task = proxy.task_get(slug=slug)

    assert task["priority"] == "p2"
    [comment] = task["comments"]
    assert "the task was closed at" in comment["body"]


@requires_omnigraph
def test_a_queued_create_is_skipped_when_that_title_was_created_since(proxy, server):
    _queue(proxy, "task_create", {"title": "only once", "description": "d"})
    server.task_create(title="only once", description="d")

    proxy.task_get(slug="tk-missing")

    hits = proxy.task_search(query="only once", repo="")
    assert [t["title"] for t in hits].count("only once") == 1
    assert not proxy.journal.has_pending()


@requires_omnigraph
def test_a_duplicate_outside_the_search_results_still_skips_the_create(proxy, server):
    # task_search ranks description matches first and stops at 20, so 21 tasks
    # that mention the title in their description push the exact-title task
    # out of it. The duplicate check must not depend on that ranking.
    title = "rotate the signing keys"
    for i in range(21):
        server.task_create(title=f"other {i}", description=f"{title} {title}")
    _queue(proxy, "task_create", {"title": title, "description": "d"})
    dup = server.task_create(title=title, description="unrelated")["slug"]
    assert dup not in [t["slug"] for t in server.task_search(query=title, repo="")]

    proxy.task_get(slug="tk-missing")

    titles = [t["title"] for t in proxy.task_list(repo="", limit=100)]
    assert titles.count(title) == 1
    assert not proxy.journal.has_pending()


@requires_omnigraph
def test_a_queued_release_naming_its_assignee_is_compared_as_that_holder(proxy):
    # task_release releases AS an explicit assignee, so a worker that claimed
    # under its own name is not "someone else" to its own release.
    slug = _new_task(proxy)
    proxy.task_claim(slug=slug, assignee="ci-worker")
    _queue(proxy, "task_release", {"slug": slug, "assignee": "ci-worker"})

    task = proxy.task_get(slug=slug)

    assert task["status"] == "open"
    assert task["assignee"] is None
    assert task["comments"] == []


@requires_omnigraph
def test_a_queued_comment_replays_as_is(proxy):
    slug = _new_task(proxy)
    _queue(proxy, "task_comment", {"slug": slug, "text": "from the outage"})

    task = proxy.task_get(slug=slug)

    assert [c["body"] for c in task["comments"]] == ["from the outage"]


def test_an_agent_on_witan_serve_gets_the_buffered_result():
    # The path an agent session actually takes: a tool on the local server
    # `witan serve` builds, forwarding to a deployment that has gone away.
    import asyncio

    from fastmcp import FastMCP
    from fastmcp.tools import FunctionTool

    from witan.remote.serve import build_remote_server

    backing: FastMCP = FastMCP("deployment")

    async def _echo(**kwargs):
        return {"saw": kwargs}

    backing.add_tool(
        FunctionTool(
            name="task_comment",
            description="from the deployment",
            parameters={
                "type": "object",
                "properties": {"slug": {"type": "string"}, "text": {"type": "string"}},
            },
            fn=_echo,
        )
    )
    p = RemoteServerProxy(REMOTE_CFG, str)
    p._new_client = lambda _token: Client(backing)  # type: ignore[method-assign]
    local = asyncio.run(build_remote_server(REMOTE_CFG, proxy=p))

    class _Refused:
        async def __aenter__(self):
            raise RuntimeError("Client failed to connect: connection refused")

        async def __aexit__(self, *_):
            return False

    p._new_client = lambda _token: _Refused()  # type: ignore[method-assign]

    async def _call():
        async with Client(local) as client:
            return await client.call_tool("task_comment", {"slug": "s", "text": "t"})

    result = asyncio.run(_call())

    assert result.data["buffered"] is True
    assert [e.args for e in p.journal.entries()] == [{"slug": "s", "text": "t"}]


def test_a_queued_cli_write_says_queued_not_failed(monkeypatch):
    from witan.cli import _common, tasks

    class _Down:
        def task_comment(self, **_kw):
            return {
                "buffered": True,
                "journal_id": "abc123",
                "pending": 2,
                "note": "witan is unreachable.",
            }

    monkeypatch.setattr(_common, "_server", _Down())
    out: list[str] = []
    monkeypatch.setattr(tasks.console, "print", lambda *a, **_kw: out.append(a[0]))

    tasks.task_comment_cmd("tk-1", "hello")

    assert "Queued" in out[0]
    assert "abc123" in out[0]


def test_journal_list_and_drop(monkeypatch):
    from witan.cli import journal as journal_cli
    from witan_core.remote.journal import WriteJournal, journal_path

    remote = REMOTE_CFG
    monkeypatch.setattr(
        journal_cli.cfg_module, "load_remote_config", lambda target=None: remote
    )
    queued = WriteJournal(journal_path(URL))
    entry, _ = queued.append(
        "task_close", {"slug": "tk-1"}, context=None, holder=HOLDER
    )
    tables: list[dict] = []
    monkeypatch.setattr(journal_cli, "render_table", lambda **kw: tables.append(kw))

    journal_cli.journal_list()
    assert [row["id"] for row in tables[0]["rows"]] == [entry.id]

    journal_cli.journal_drop(entry.id)
    assert not queued.has_pending()
    with pytest.raises(SystemExit):
        journal_cli.journal_drop(entry.id)


def test_journal_refuses_without_a_deployment(monkeypatch):
    from witan.cli import journal as journal_cli

    monkeypatch.setattr(
        journal_cli.cfg_module, "load_remote_config", lambda target=None: None
    )
    with pytest.raises(SystemExit):
        journal_cli.journal_list()


def test_the_context_line_names_the_queue_only_when_there_is_one():
    from witan import context
    from witan_core.remote.journal import WriteJournal, journal_path

    assert context.journal_line(URL) == ""
    queued = WriteJournal(journal_path(URL))
    for slug in ("tk-1", "tk-2"):
        queued.append("task_close", {"slug": slug}, context=None, holder=None)

    line = context.journal_line(URL)

    assert line.startswith("2 witan write(s) queued since ")
    assert "`witan journal`" in line
