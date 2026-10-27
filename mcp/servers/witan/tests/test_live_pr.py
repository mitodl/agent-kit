"""Live linked-PR state (witan/live_pr.py) and where it is rendered.

Stored task and project text drifts; the linked PR's state is the live signal.
These run a stub ``gh`` placed first on ``PATH``, so nothing reaches GitHub.
"""

from __future__ import annotations

import asyncio
import json
import os
import textwrap

import pytest

from witan import live_pr, session_state

from .conftest import requires_omnigraph
from .test_context import _FakeRemoteServer, _git, _git_repo, _setup, _unwrap

PR_URL = "https://github.com/o/r/pull/512"
PR_FIELDS = "number,state,isDraft,reviewDecision,mergedAt,statusCheckRollup"

# `exec`, so a timeout's kill reaches the sleeping process itself. A child of
# the shell would keep the stdout pipe open and hold `subprocess.run` past it.
_STUB = textwrap.dedent(
    """\
    #!/bin/sh
    echo "$@" >> "$STUB_GH_LOG"
    case "$STUB_GH_MODE" in
      fail) echo "To get started with GitHub CLI, please run: gh auth login" >&2
            exit 1 ;;
      hang) exec sleep 30 ;;
    esac
    cat "$STUB_GH_OUT"
    """
)

_OPEN_DRAFT_PR = {
    "number": 512,
    "state": "OPEN",
    "isDraft": True,
    "reviewDecision": "CHANGES_REQUESTED",
    "mergedAt": None,
    "statusCheckRollup": [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""},
        {"__typename": "StatusContext", "state": "PENDING"},
    ],
}


class _Gh:
    def __init__(self, tmp_path, monkeypatch):
        self._log = tmp_path / "gh.log"
        self._out = tmp_path / "gh.json"
        self._monkeypatch = monkeypatch
        monkeypatch.setenv("STUB_GH_LOG", str(self._log))
        monkeypatch.setenv("STUB_GH_OUT", str(self._out))
        monkeypatch.setenv("STUB_GH_MODE", "ok")
        self.answer(_OPEN_DRAFT_PR)

    def answer(self, payload: dict) -> None:
        self._out.write_text(json.dumps(payload))

    def mode(self, mode: str) -> None:
        self._monkeypatch.setenv("STUB_GH_MODE", mode)

    def calls(self) -> list[str]:
        return self._log.read_text().splitlines() if self._log.exists() else []


@pytest.fixture
def gh(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(_STUB)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("WITAN_LIVE_PR", "1")
    # hermetic.py does not redirect the temp dir, which is where the cache is.
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(session_state, "session_state_dir", lambda: state)
    return _Gh(tmp_path, monkeypatch)


def test_a_pr_reports_its_state_review_and_checks(gh):
    state = live_pr.live_state(PR_URL)

    assert gh.calls() == [f"pr view {PR_URL} --json {PR_FIELDS}"]
    assert state["kind"] == "pr"
    assert state["ref"] == "o/r#512"
    assert state["state"] == "open"
    assert state["draft"] is True
    assert state["review_decision"] == "CHANGES_REQUESTED"
    assert state["checks"] == {"pass": 2, "fail": 1, "pending": 2}
    assert state["fetched_at"]

    line = live_pr.describe(state)
    assert line.startswith(
        "PR o/r#512: open (draft), review: CHANGES_REQUESTED, "
        "checks: 2 pass / 1 fail / 2 pending (live, "
    )
    # UTC, like the comment dates the hook shows beside it.
    assert line.endswith(" UTC)")


def test_a_merged_pr_is_merged_not_closed(gh):
    gh.answer({**_OPEN_DRAFT_PR, "state": "MERGED", "isDraft": False})
    state = live_pr.live_state(PR_URL)
    assert state["state"] == "merged"
    assert live_pr.describe(state).startswith("PR o/r#512: merged, ")


def test_an_issue_url_without_a_scheme_is_looked_up_as_an_issue(gh):
    gh.answer({"number": 7, "state": "CLOSED", "closedAt": "2026-10-01T00:00:00Z"})
    state = live_pr.live_state("github.com/o/r/issues/7#issuecomment-1")

    assert gh.calls() == [
        "issue view https://github.com/o/r/issues/7 --json number,state,closedAt"
    ]
    assert state["kind"] == "issue"
    assert state["state"] == "closed"
    assert live_pr.describe(state).startswith("Issue o/r#7: closed (live, ")


def test_a_url_that_is_not_github_is_an_error_without_calling_gh(gh):
    state = live_pr.live_state("https://jira.example.org/browse/OPS-1")
    assert "not a GitHub PR or issue URL" in state["error"]
    assert live_pr.describe(state) is None
    assert gh.calls() == []


def test_gh_missing_is_an_error(gh, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert live_pr.live_state(PR_URL) == {"error": "gh is not on PATH"}


def test_an_unauthenticated_gh_is_an_error_naming_why(gh):
    gh.mode("fail")
    state = live_pr.live_state(PR_URL)
    assert state["error"].startswith("gh exited 1: To get started")


def test_a_gh_that_hangs_is_cut_off(gh, monkeypatch):
    monkeypatch.setattr(live_pr, "_GH_TIMEOUT_SECONDS", 0.5)
    gh.mode("hang")
    assert live_pr.live_state(PR_URL) == {"error": "gh timed out after 0.5s"}


def test_a_second_lookup_inside_the_ttl_is_served_from_cache(gh, monkeypatch):
    first = live_pr.live_state(PR_URL)
    second = live_pr.live_state(f"{PR_URL}/files")
    assert len(gh.calls()) == 1
    # The cached answer keeps the time it was fetched, not the time it was
    # read back, so the rendered "(live, HH:MM UTC)" stays honest.
    assert second == first

    monkeypatch.setattr(live_pr, "_CACHE_TTL_SECONDS", 0)
    live_pr.live_state(PR_URL)
    assert len(gh.calls()) == 2


def _plant(url: str, stamp: float, state: object) -> None:
    live_pr._cache_file(url).write_text(json.dumps({"stamp": stamp, "state": state}))


@pytest.mark.parametrize(
    "stamp_offset, state",
    [
        # A stamp in the future would otherwise be served forever.
        (1e9, None),
        # Shapes `describe` would crash on, or that name another URL.
        (0, {}),
        (0, "merged"),
        (0, {"kind": "pr", "ref": "o/r#512", "state": "merged"}),
        (0, {"kind": "pr", "ref": "o/r#512", "state": "merged", "url": "x"}),
    ],
)
def test_a_planted_or_malformed_cache_entry_is_a_miss(gh, stamp_offset, state):
    """The cache lives at a predictable name in a shared temp dir."""
    import time

    forged = {
        "kind": "pr",
        "url": PR_URL,
        "ref": "o/r#512",
        "state": "merged",
        "checks": {"pass": 9},
        "fetched_at": "2026-10-23T10:00:00+00:00",
    }
    _plant(PR_URL, time.time() + stamp_offset, forged if state is None else state)

    assert live_pr.live_state(PR_URL)["state"] == "open"
    assert len(gh.calls()) == 1


def test_a_cache_entry_another_user_owns_is_a_miss(gh, monkeypatch):
    live_pr.live_state(PR_URL)
    monkeypatch.setattr(os, "getuid", lambda: os.geteuid() + 1)
    live_pr.live_state(PR_URL)
    assert len(gh.calls()) == 2


def test_a_failed_lookup_is_not_cached(gh):
    gh.mode("fail")
    assert "error" in live_pr.live_state(PR_URL)
    gh.mode("ok")
    assert live_pr.live_state(PR_URL)["state"] == "open"
    assert len(gh.calls()) == 2


def test_annotate_adds_live_pr_only_for_a_github_url(gh):
    annotated = live_pr.annotate({"external_uri": PR_URL}, "external_uri")
    assert annotated["live_pr"]["state"] == "open"
    other = {"external_uri": "https://jira.example.org/browse/OPS-1"}
    assert live_pr.annotate(other, "external_uri") == other
    assert live_pr.annotate({"external_uri": None}, "external_uri") == {
        "external_uri": None
    }
    assert live_pr.annotate(None, "external_uri") is None


def test_annotate_is_off_under_witan_live_pr_0(gh, monkeypatch):
    monkeypatch.setenv("WITAN_LIVE_PR", "0")
    record = {"external_uri": PR_URL}
    assert live_pr.annotate(record, "external_uri") == record
    assert gh.calls() == []


def test_tool_results_are_annotated_only_once_a_local_server_opts_in(gh, monkeypatch):
    # The deployment serves these same tool functions; it must not run `gh`.
    record = {"external_uri": PR_URL}
    assert live_pr.tool_result("task_get", record) == record

    monkeypatch.setattr(live_pr, "_annotate_tool_results", True)
    assert "live_pr" in live_pr.tool_result("task_get", record)
    assert "live_pr" in live_pr.tool_result(
        "workflow_project_get", {"github_pr": PR_URL}
    )


def test_the_remote_forwarder_annotates_task_get_locally(gh):
    from witan.remote.serve import _forwarder

    class _Proxy:
        async def dispatch(self, name, /, **kwargs):
            return {"slug": kwargs["slug"], "external_uri": PR_URL}

    result = asyncio.run(_forwarder(_Proxy(), "task_get")(slug="tk-x"))
    assert result["live_pr"]["ref"] == "o/r#512"

    plain = asyncio.run(_forwarder(_Proxy(), "task_list")(slug="tk-x"))
    assert "live_pr" not in plain


# ── The context hook ──────────────────────────────────────────────────────────


def _remote_env(tmp_path, monkeypatch, repo="https://github.com/test/live"):
    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    monkeypatch.setenv("WITAN_REPO", repo)
    return repo


def _server(**overrides):
    defaults = {
        "projects": [
            {
                "slug": "wp-live",
                "title": "Live Project",
                "phase": "implementation",
                "github_pr": "https://github.com/o/r/pull/600",
            }
        ],
        "ready": [],
        "sessions_by_project": {},
        "held": [
            {
                "slug": "tk-held",
                "title": "Held Task",
                "status": "in_progress",
                "external_uri": PR_URL,
            }
        ],
    }
    return _FakeRemoteServer(**{**defaults, **overrides})


def test_the_hook_shows_live_state_under_the_project_and_held_task(
    gh, tmp_path, monkeypatch
):
    from witan import context as ctx_module

    _remote_env(tmp_path, monkeypatch)
    text = ctx_module.inject_context_remote(_server(), "https://witan.example.org")

    assert "## Work You Hold" in text
    held_block = text.split("## Work You Hold")[1].split("##")[0]
    assert "Held Task" in held_block
    assert "PR o/r#512: open (draft)" in held_block
    projects_block = text.split("## Active Workflow Projects")[1]
    assert "PR o/r#512" not in projects_block
    assert "PR o/r#600: open (draft)" in projects_block
    assert "(live, " in projects_block
    assert sorted(gh.calls()) == [
        f"pr view {u} --json {PR_FIELDS}"
        for u in (PR_URL, "https://github.com/o/r/pull/600")
    ]


def test_the_hook_renders_nothing_for_a_failed_lookup(gh, tmp_path, monkeypatch):
    from witan import context as ctx_module

    _remote_env(tmp_path, monkeypatch)
    gh.mode("fail")
    text = ctx_module.inject_context_remote(_server(), "https://witan.example.org")

    assert "(live, " not in text
    # Nothing to say about a held task without its live line.
    assert "## Work You Hold" not in text
    assert "Live Project" in text


def test_the_hook_puts_the_line_under_a_commented_held_task(gh, tmp_path, monkeypatch):
    from witan import context as ctx_module

    _remote_env(tmp_path, monkeypatch)
    comments = {
        "tk-held": [
            {
                "slug": "tc-1",
                "author": "alice",
                "body": "blocked on sign-off",
                "created_at": "2026-10-15T20:09:40.226",
            }
        ]
    }
    text = ctx_module.inject_context_remote(
        _server(comments=comments, projects=[]), "https://witan.example.org"
    )

    block = text.split("## ⚠ New Comments on Work You Hold")[1]
    assert "PR o/r#512: open (draft)" in block
    # The comment carries its date, since it is a snapshot from then.
    assert "alice (2026-10-15 20:09 UTC): blocked on sign-off" in block
    assert "## Work You Hold" not in text


def test_the_hook_caps_its_lookups(gh, tmp_path, monkeypatch):
    from witan import context as ctx_module

    _remote_env(tmp_path, monkeypatch)
    branch_tasks = [
        {
            "slug": f"tk-b{n}",
            "title": f"Branch {n}",
            "status": "in_progress",
            "external_uri": f"https://github.com/o/r/pull/{n}",
        }
        for n in range(10)
    ]
    monkeypatch.setattr(
        ctx_module, "_cached_repo_and_branch", lambda: ("https://x/r", "feat")
    )
    ctx_module.inject_context_remote(
        _server(branch_tasks=branch_tasks), "https://witan.example.org"
    )
    assert len(gh.calls()) == ctx_module._LIVE_LOOKUP_LIMIT


def test_the_hook_makes_no_lookups_when_turned_off(gh, tmp_path, monkeypatch):
    from witan import context as ctx_module

    _remote_env(tmp_path, monkeypatch)
    monkeypatch.setenv("WITAN_LIVE_PR", "0")
    text = ctx_module.inject_context_remote(_server(), "https://witan.example.org")
    assert gh.calls() == []
    assert "Live Project" in text


@requires_omnigraph
def test_the_local_hook_shows_live_state_for_the_in_flight_branch_task(
    gh, tmp_path, monkeypatch
):
    """The local path, which also needs `code_branch_tasks` to select the URL."""
    from witan import context as ctx_module
    from witan import server as srv

    monkeypatch.setenv("WITAN_CONTEXT_TTL", "0")
    store, queries_dir = _setup(tmp_path, monkeypatch, "https://github.com/test/lb")
    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/live")
    monkeypatch.chdir(base)

    task = _unwrap(srv.task_create)(
        title="linked work", description="x", external_uri=PR_URL
    )
    _unwrap(srv.task_claim)(task["slug"])

    text = ctx_module.inject_context(str(store), queries_dir, None)
    block = text.split("## In-Flight Branch")[1]
    assert "linked work" in block
    assert "PR o/r#512: open (draft)" in block


@requires_omnigraph
def test_the_local_hook_does_not_cache_a_lookup_cut_off_by_the_deadline(
    gh, tmp_path, monkeypatch
):
    """Cached, the render would hide the PR line for the whole TTL, when the
    next prompt's lookup might well answer."""
    from witan import context as ctx_module
    from witan import server as srv

    monkeypatch.setenv("WITAN_CONTEXT_DEADLINE", "1")
    monkeypatch.setattr(live_pr, "_GH_TIMEOUT_SECONDS", 2)
    gh.mode("hang")
    store, queries_dir = _setup(tmp_path, monkeypatch, "https://github.com/test/lt")
    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/slow")
    monkeypatch.chdir(base)

    task = _unwrap(srv.task_create)(
        title="linked work", description="x", external_uri=PR_URL
    )
    _unwrap(srv.task_claim)(task["slug"])

    text = ctx_module.inject_context(str(store), queries_dir, None)
    assert "linked work" in text
    assert "witan context partial: linked PR state timed out after 1s" in text
    repo, branch = ctx_module._cached_repo_and_branch()
    assert ctx_module._read_output_cache(str(store), repo, branch) is None


@requires_omnigraph
def test_project_status_carries_how_old_the_project_text_is(tmp_path, monkeypatch):
    """`witan project status` prints it, so the rollup has to send it."""
    from witan import server as srv

    _setup(tmp_path, monkeypatch, "https://github.com/test/age")
    project = _unwrap(srv.workflow_project_create)(title="P", description="d")
    status = _unwrap(srv.workflow_project_status)(slug=project["slug"])
    assert status["project"]["updated_at"]


def test_the_cli_prints_the_live_line_and_a_failed_lookup(gh, monkeypatch):
    from witan.cli import _common

    printed: list[str] = []
    monkeypatch.setattr(
        _common.console, "print", lambda *a, **k: printed.append(str(a[0]))
    )
    _common.print_live_pr(live_pr.annotate({"external_uri": PR_URL}, "external_uri"))
    gh.mode("fail")
    monkeypatch.setattr(live_pr, "_CACHE_TTL_SECONDS", 0)
    _common.print_live_pr(live_pr.annotate({"external_uri": PR_URL}, "external_uri"))
    _common.print_live_pr({})

    assert len(printed) == 2
    assert "PR o/r#512: open (draft)" in printed[0]
    assert "live state unavailable: gh exited 1" in printed[1]


def test_the_stdio_server_opts_in(monkeypatch):
    """`witan serve --transport stdio` is the switch for the tool functions."""
    from witan import cli

    monkeypatch.setattr(live_pr, "_annotate_tool_results", False)
    monkeypatch.setattr(cli, "_serve_target", lambda transport: _NoRun())
    cli.serve(transport="stdio")
    assert live_pr._annotate_tool_results is True


class _NoRun:
    def mount(self, _other):
        pass

    def run(self, *args, **kwargs):
        pass


def test_the_shared_test_guard_keeps_lookups_off():
    """Every test runs with lookups off unless it turns them on (conftest)."""
    assert os.environ["WITAN_LIVE_PR"] == "0"
