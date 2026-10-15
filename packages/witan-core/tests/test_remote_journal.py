"""The write journal: queue a write the deployment never received, replay it later.

The proxy here is scripted rather than pointed at a server, because what is
under test is WHERE a failure happens (before the connection opened vs. after
the call went out) and what the proxy does about each, not any tool's
behaviour. The witan binding's replay conflicts (a task claimed or closed
since) are exercised against real tools in witan-council's suite.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
from types import SimpleNamespace

import httpx2
import pytest

from witan_core.remote.journal import WriteJournal, journal_dir, journal_path
from witan_core.remote.proxy import (
    RemoteCredentialRejected,
    RemoteMCPProxy,
    RemoteNotSent,
    RemoteUnreachable,
    RemoteWriteIndeterminate,
)

ENDPOINT = "https://witan.example.org/mcp"


def _jwt(**claims) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def _status_error(status: int) -> httpx2.HTTPStatusError:
    request = httpx2.Request("POST", ENDPOINT)
    response = httpx2.Response(status, request=request, text="nope")
    return httpx2.HTTPStatusError(
        f"error '{status}'", request=request, response=response
    )


@pytest.fixture(autouse=True)
def _data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


class _Deployment:
    """A scripted deployment: which phase fails, and every call that reached it."""

    def __init__(self):
        self.fail_at: str | None = None
        self.exc: BaseException | None = None
        self.fail_tools: dict[str, BaseException] = {}
        self.calls: list[tuple[str, dict]] = []

    def client(self):
        deployment = self

        class _Client:
            async def __aenter__(self):
                if deployment.fail_at == "connect":
                    raise deployment.exc
                return self

            async def __aexit__(self, *_):
                return False

            async def call_tool(self, name, arguments):
                deployment.calls.append((name, arguments))
                if name in deployment.fail_tools:
                    raise deployment.fail_tools[name]
                if deployment.fail_at == "call":
                    raise deployment.exc
                return SimpleNamespace(data={"tool": name, **arguments})

        return _Client()


class _Proxy(RemoteMCPProxy):
    """Buffers `task_comment` when nothing was sent, `task_close` either way."""

    def __init__(self, deployment, *, url=ENDPOINT, repo="https://github.com/o/r"):
        super().__init__(url, lambda: _jwt(preferred_username="ada@example.org"))
        self._deployment = deployment
        self.repo = repo
        self._param_names = {
            "task_get": ["slug"],
            "task_comment": ["slug", "text"],
            "task_close": ["resolution", "slug"],
            "task_create": ["repo", "title"],
        }

    def _new_client(self, token):
        return self._deployment.client()

    def _writes(self, name):
        return name != "task_get"

    def _buffers(self, name, *, dispatched):
        if dispatched:
            return name == "task_close"
        return name in {"task_comment", "task_close", "task_create"}

    def _resolve_repo(self):
        return self.repo


def _down(deployment: _Deployment) -> None:
    deployment.fail_at = "connect"
    deployment.exc = RuntimeError("Client failed to connect: connection refused")


def _up(deployment: _Deployment) -> None:
    deployment.fail_at = None
    deployment.exc = None


def test_a_write_that_never_connected_is_queued_and_answered_not_raised():
    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment)

    result = proxy.task_comment(slug="tk-1", text="hello")

    assert result["buffered"] is True
    assert result["pending"] == 1
    assert "Don't retry it" in result["note"]
    [entry] = proxy.journal.entries()
    assert entry.id == result["journal_id"]
    assert (entry.tool, entry.args) == (
        "task_comment",
        {"slug": "tk-1", "text": "hello"},
    )
    assert entry.holder == "ada@example.org"
    assert deployment.calls == []


def test_a_connect_failure_is_a_not_sent_unreachable():
    # RemoteNotSent narrows RemoteUnreachable, so every existing handler of
    # the wider class still catches it.
    deployment = _Deployment()
    _down(deployment)
    with pytest.raises(RemoteNotSent) as caught:
        _Proxy(deployment).task_get(slug="tk-1")
    assert isinstance(caught.value, RemoteUnreachable)


def test_a_read_during_an_outage_still_raises():
    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment)
    with pytest.raises(RemoteUnreachable):
        proxy.task_get(slug="tk-1")
    assert not proxy.journal.has_pending()


def test_a_drop_after_the_call_went_out_is_not_queued():
    # The request may have been applied, so queueing it risks a duplicate.
    deployment = _Deployment()
    deployment.fail_at = "call"
    deployment.exc = ExceptionGroup("x", [httpx2.ReadError("reset by peer")])
    proxy = _Proxy(deployment)
    with pytest.raises(RemoteUnreachable) as caught:
        proxy.task_comment(slug="tk-1", text="hello")
    assert not isinstance(caught.value, RemoteNotSent)
    assert not proxy.journal.has_pending()


def test_a_gateway_cut_queues_only_an_idempotent_write():
    deployment = _Deployment()
    deployment.fail_at = "call"
    deployment.exc = ExceptionGroup("x", [_status_error(502)])
    proxy = _Proxy(deployment)

    assert proxy.task_close(slug="tk-1")["buffered"] is True
    with pytest.raises(RemoteWriteIndeterminate):
        proxy.task_comment(slug="tk-1", text="hello")
    assert [e.tool for e in proxy.journal.entries()] == ["task_close"]


def test_a_rejected_credential_at_connect_is_not_queued():
    deployment = _Deployment()
    deployment.fail_at = "connect"
    deployment.exc = _status_error(401)
    proxy = _Proxy(deployment)
    with pytest.raises(RemoteCredentialRejected):
        proxy.task_comment(slug="tk-1", text="hello")
    assert not proxy.journal.has_pending()


def test_the_next_successful_call_replays_in_order_before_running():
    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment)
    proxy.task_comment(slug="tk-1", text="first")
    proxy.task_close(slug="tk-1", resolution="done")

    _up(deployment)
    proxy.task_get(slug="tk-2")

    assert [name for name, _ in deployment.calls] == [
        "task_comment",
        "task_close",
        "task_get",
    ]
    assert deployment.calls[0][1] == {"slug": "tk-1", "text": "first"}
    assert not proxy.journal.has_pending()


def test_replay_sends_the_repo_resolved_when_the_write_was_made():
    # A replay can run from another checkout; it must not re-scope the write.
    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment, repo="https://github.com/o/first")
    proxy.task_create(title="t")

    _up(deployment)
    proxy.repo = "https://github.com/o/elsewhere"
    proxy.task_get(slug="tk-2")

    assert deployment.calls[0] == (
        "task_create",
        {"title": "t", "repo": "https://github.com/o/first"},
    )


def test_a_retryable_write_that_fails_on_replay_is_kept_with_the_rest():
    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment)
    proxy.task_close(slug="tk-1")
    proxy.task_comment(slug="tk-1", text="second")

    _up(deployment)
    deployment.fail_tools["task_close"] = ExceptionGroup(
        "x", [httpx2.ReadError("reset")]
    )
    report = asyncio.run(proxy.replay_journal())

    assert [line["outcome"] for line in report] == ["kept"]
    assert [e.tool for e in proxy.journal.entries()] == ["task_close", "task_comment"]


@pytest.mark.parametrize(
    "failure",
    [
        ExceptionGroup("x", [httpx2.ReadError("reset after send")]),
        ExceptionGroup("x", [_status_error(504)]),
    ],
    ids=["drop", "gateway"],
)
def test_a_non_idempotent_write_that_fails_on_replay_is_never_resent(failure):
    # The request may have landed, so a second send could write a second
    # comment. It replays at most once, and replay stops behind it.
    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment)
    proxy.task_comment(slug="tk-1", text="maybe")
    proxy.task_comment(slug="tk-1", text="later")

    _up(deployment)
    deployment.fail_tools["task_comment"] = failure
    report = asyncio.run(proxy.replay_journal())

    assert [line["outcome"] for line in report] == ["lost"]
    assert [e.args["text"] for e in proxy.journal.entries()] == ["later"]


def test_a_non_idempotent_write_is_off_the_journal_before_it_is_sent():
    # Covers what no except clause can: a replay cancelled mid-send.
    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment)
    proxy.task_comment(slug="tk-1", text="once")

    _up(deployment)
    deployment.fail_tools["task_comment"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(proxy.replay_journal())

    assert not proxy.journal.has_pending()


def test_a_failed_pre_read_keeps_the_entry(monkeypatch):
    # Nothing was written, so the entry is as safe to send next time.
    from fastmcp.exceptions import ToolError

    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment)
    proxy.task_comment(slug="tk-1", text="queued")

    async def plan(entry, call):
        await call("task_get", {"slug": "tk-1"})
        return entry

    monkeypatch.setattr(proxy, "_replay_plan", plan)
    _up(deployment)
    deployment.fail_tools["task_get"] = ToolError("backend warming up")
    report = asyncio.run(proxy.replay_journal())

    assert [line["outcome"] for line in report] == ["kept"]
    assert len(proxy.journal.entries()) == 1


def test_buffering_can_be_turned_off_for_a_caller_that_needs_the_error():
    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment)
    proxy._buffer_writes = False
    with pytest.raises(RemoteNotSent):
        proxy.task_comment(slug="tk-1", text="hello")
    assert not proxy.journal.has_pending()


def test_the_journal_is_private_to_its_owner():
    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment)
    proxy.task_comment(slug="tk-1", text="secret-ish")
    assert proxy.journal.path.stat().st_mode & 0o777 == 0o600
    assert proxy.journal.path.parent.stat().st_mode & 0o077 == 0


def test_a_refused_entry_is_dropped_and_replay_continues():
    # A refusal is an answer; keeping it at the head would wedge the queue.
    from fastmcp.exceptions import ToolError

    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment)
    proxy.task_close(slug="tk-gone")
    proxy.task_comment(slug="tk-1", text="after")

    _up(deployment)
    deployment.fail_tools["task_close"] = ToolError("no such task")
    report = asyncio.run(proxy.replay_journal())

    assert [line["outcome"] for line in report] == ["dropped", "applied"]
    assert not proxy.journal.has_pending()


def test_a_held_replay_lock_means_this_caller_does_not_replay():
    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment)
    proxy.task_comment(slug="tk-1", text="queued")

    _up(deployment)
    with proxy.journal.replaying() as acquired:
        assert acquired
        # A second holder (another process, or another coroutine in this one)
        # opens its own descriptor, which flock refuses while the first holds.
        with proxy.journal.replaying() as second:
            assert not second
        proxy.task_get(slug="tk-2")

    assert [name for name, _ in deployment.calls] == ["task_get"]
    assert len(proxy.journal.entries()) == 1


def test_a_custom_replay_plan_can_skip_or_substitute(monkeypatch):
    deployment = _Deployment()
    _down(deployment)
    proxy = _Proxy(deployment)
    proxy.task_create(title="dup")
    proxy.task_close(slug="tk-1")

    async def plan(entry, call):
        await call("task_get", {"slug": "tk-1"})
        if entry.tool == "task_create":
            return None
        return dataclasses.replace(
            entry, tool="task_comment", args={"slug": "tk-1", "text": "x"}
        )

    monkeypatch.setattr(proxy, "_replay_plan", plan)
    _up(deployment)
    report = asyncio.run(proxy.replay_journal())

    assert [line["outcome"] for line in report] == ["skipped", "substituted"]
    assert [name for name, _ in deployment.calls] == [
        "task_get",
        "task_get",
        "task_comment",
    ]


def test_each_deployment_has_its_own_journal():
    deployment = _Deployment()
    _down(deployment)
    _Proxy(deployment, url="https://a.example.org/mcp").task_comment(slug="s", text="a")
    other = _Proxy(deployment, url="https://b.example.org/mcp")
    assert not other.journal.has_pending()
    assert journal_path("https://a.example.org/mcp") != journal_path(
        "https://b.example.org/mcp"
    )


def test_the_journal_lives_under_the_data_home_read_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "elsewhere"))
    assert journal_dir() == tmp_path / "elsewhere" / "witan" / "journal"


def test_remove_reports_whether_the_entry_existed(tmp_path):
    journal = WriteJournal(tmp_path / "j.jsonl")
    entry, pending = journal.append(
        "task_comment", {"slug": "s"}, context=None, holder=None
    )
    assert pending == 1
    assert journal.remove(entry.id) is True
    assert journal.remove(entry.id) is False
    assert not journal.has_pending()
