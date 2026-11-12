"""The summary the Stop hook writes when it auto-closes a session.

The contract (tk-build-auto-closed-session-summaries-from-recorde-b9f335): a
session with real activity shows it in ``workflow_project_status``'s handoff,
a session with none gets a short honest line, a failed read still closes the
session, and an explicit ``workflow_session_end`` summary is never replaced.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import UTC, datetime

import pytest

from witan import readiness, session_state, session_summary

from .conftest import requires_omnigraph

SID = "aaaaaaaa-1111-2222-3333-444444444444"
OTHER_SID = "bbbbbbbb-1111-2222-3333-444444444444"
SINCE = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A checkout whose author is the hermetic ``$GIT_AUTHOR_EMAIL``."""
    path = tmp_path / "checkout"
    path.mkdir()
    _git(path, "init", "-q", "-b", "feat/thing")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit(repo, name, message, *, email=None, date=None):
    (repo / name).write_text(name)
    _git(repo, "add", name)
    env = dict(os.environ)
    if email:
        env["GIT_AUTHOR_EMAIL"] = email
    if date:
        env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", message],
        check=True,
        capture_output=True,
        env=env,
    )


# ── render ───────────────────────────────────────────────────────────────────


def test_no_activity_is_a_short_honest_line():
    text = session_summary.render(session_summary.Activity())
    assert text == session_summary.NO_ACTIVITY
    assert "\n" not in text


def test_no_activity_with_a_failed_read_is_the_placeholder():
    """ "Nothing happened" would be unchecked if a source couldn't be read."""
    activity = session_summary.Activity(unread=["tasks"])
    assert session_summary.render(activity) == session_summary.PLACEHOLDER


def test_every_variant_reads_as_an_auto_close():
    """The server's ``_has_own_summary`` keys on this prefix."""
    busy = session_summary.Activity(commits=["abc1234 fix it"])
    for activity in (session_summary.Activity(), busy):
        assert session_summary.render(activity).startswith(
            session_summary.AUTOCLOSE_PREFIX
        )


def test_activity_is_listed_and_failed_sources_are_named():
    activity = session_summary.Activity(
        closed_tasks=[{"slug": "tk-a", "title": "Fix A", "status": "closed"}],
        claimed_tasks=[{"slug": "tk-b", "title": "Build B", "status": "in_progress"}],
        memories=[{"slug": "les-c", "title": "Watch C"}],
        branch="feat/thing",
        commits=["abc1234 fix: A"],
        changed_files=["a.py", "b.py"],
        unread=["memories"],
    )
    text = session_summary.render(activity)
    assert "Closed tasks: tk-a (Fix A)" in text
    assert "Claimed tasks: tk-b (Build B) [in_progress]" in text
    assert "Memories stored: les-c (Watch C)" in text
    assert "Branch: feat/thing" in text
    assert "Commits: abc1234 fix: A" in text
    assert "Uncommitted changes in 2 files: a.py; b.py" in text
    assert "Could not read: memories." in text


def test_long_lists_are_capped_with_a_count():
    commits = [f"{i:07x} commit {i}" for i in range(session_summary.LIST_CAP + 3)]
    text = session_summary.render(session_summary.Activity(commits=commits))
    assert "and 3 more" in text
    assert commits[-1] not in text


# ── task attribution ─────────────────────────────────────────────────────────


def _task(slug, holder_sid, status, *, claimed_at=None, closed_at=None):
    return {
        "slug": slug,
        "title": slug,
        "status": status,
        "assignee": f"someone@example.org#{readiness.session_suffix(holder_sid)}",
        "claimed_at": claimed_at,
        "closed_at": closed_at,
    }


def test_tasks_are_this_sessions_by_holder_suffix_and_window():
    inside = "2026-11-12T15:30:00.000"  # naive, as the store returns it
    before = "2026-11-12T14:00:00.000"
    rows = [
        _task("tk-closed", SID, "closed", claimed_at=before, closed_at=inside),
        _task("tk-claimed", SID, "in_progress", claimed_at=inside),
        _task("tk-old", SID, "in_progress", claimed_at=before),
        _task("tk-old-close", SID, "closed", claimed_at=before, closed_at=before),
        _task("tk-theirs", OTHER_SID, "in_progress", claimed_at=inside),
        {"slug": "tk-free", "status": "open", "assignee": None},
    ]
    closed, claimed = session_summary.session_tasks(rows, SID, SINCE)
    assert [r["slug"] for r in closed] == ["tk-closed"]
    assert [r["slug"] for r in claimed] == ["tk-claimed"]


def test_no_session_id_attributes_nothing():
    rows = [_task("tk-x", SID, "in_progress", claimed_at="2026-11-12T16:00:00")]
    assert session_summary.session_tasks(rows, "", SINCE) == ([], [])


# ── git ──────────────────────────────────────────────────────────────────────


def test_git_activity_reads_the_window_and_the_author(repo):
    _commit(repo, "old", "old work", date="2026-11-12T14:00:00+00:00")
    _commit(repo, "mine", "fix: mine", date="2026-11-12T15:30:00+00:00")
    _commit(
        repo,
        "theirs",
        "pulled in",
        email="someone@example.org",
        date="2026-11-12T15:40:00+00:00",
    )
    (repo / "mine").write_text("edited")

    activity = session_summary.Activity()
    session_summary.git_activity(activity, str(repo), SINCE)

    assert activity.branch == "feat/thing"
    assert [c.split(" ", 1)[1] for c in activity.commits] == ["fix: mine"]
    assert activity.changed_files == ["mine"]
    assert activity.unread == []


def test_a_directory_that_is_not_a_checkout_has_no_git_activity(tmp_path):
    """Not a failed read: the session can still be reported as idle."""
    activity = session_summary.Activity()
    session_summary.git_activity(activity, str(tmp_path), SINCE)
    assert activity == session_summary.Activity()


def test_untracked_files_count_as_uncommitted_changes(repo):
    """A session that only created files left work behind."""
    _commit(repo, "base", "base")
    (repo / "new.py").write_text("x")
    (repo / ".gitignore").write_text("ignored.log\n")
    (repo / "ignored.log").write_text("x")

    activity = session_summary.Activity()
    session_summary.git_activity(activity, str(repo), SINCE)

    assert sorted(activity.changed_files) == [".gitignore", "new.py"]
    assert session_summary.changed_files(str(repo)) == activity.changed_files


@pytest.mark.parametrize(
    ("failing", "source"),
    [("diff", "uncommitted changes"), ("rev-parse --abbrev-ref", "branch")],
)
def test_a_failed_git_read_is_named_not_read_as_empty(
    repo, monkeypatch, failing, source
):
    _commit(repo, "base", "base", date="2026-11-12T14:00:00+00:00")
    real_git = session_summary._git

    def flaky(project_dir, *args):
        if " ".join(args).startswith(failing):
            raise subprocess.TimeoutExpired(["git", *args], 5)
        return real_git(project_dir, *args)

    monkeypatch.setattr(session_summary, "_git", flaky)
    activity = session_summary.Activity()
    session_summary.git_activity(activity, str(repo), SINCE)

    assert activity.unread == [source]
    assert session_summary.render(activity) == session_summary.PLACEHOLDER


def test_no_author_identity_leaves_commits_unread(repo, monkeypatch):
    """Without an author to filter on, `git log` would credit everyone's commits."""
    _commit(repo, "theirs", "someone else's", date="2026-11-12T15:30:00+00:00")
    monkeypatch.setattr(session_summary, "_author_email", lambda _dir: None)

    activity = session_summary.Activity()
    session_summary.git_activity(activity, str(repo), SINCE)

    assert activity.commits == []
    assert activity.unread == ["commits"]


# ── the Stop hook, end to end ────────────────────────────────────────────────


@pytest.fixture
def hook(server, monkeypatch):
    from witan import server as srv
    from witan.cli import _common, hooks

    monkeypatch.setattr(_common, "_server", srv)
    monkeypatch.setenv("CLAUDE_SESSION_ID", SID)
    return hooks


def _handoff(server, project_slug):
    return server.workflow_project_status(project_slug)["last_session"]


@requires_omnigraph
def test_auto_close_shows_the_sessions_activity_in_the_handoff(
    server, hook, monkeypatch, repo
):
    proj = server.workflow_project_create(title="busy", description="d")
    slug = proj["slug"]
    handle = server.workflow_session_start(
        project_slug=slug, session_id=SID, phase="implementation"
    )
    claimed = server.task_create(title="Keep going", description="x", project_slug=slug)
    closed = server.task_create(title="Finish up", description="x", project_slug=slug)
    theirs = server.task_create(title="Not mine", description="x", project_slug=slug)
    server.task_claim(claimed["slug"], session_id=SID)
    server.task_claim(closed["slug"], session_id=SID)
    server.task_close(closed["slug"], resolution="done")
    server.task_claim(theirs["slug"], session_id=OTHER_SID)
    mem = server.memory_store(
        kind="lesson",
        title="Mind the gap",
        content="c",
        session_slug=handle["session_slug"],
    )
    _commit(repo, "work", "feat: the work")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))

    hook.session_checkpoint()

    last = _handoff(server, slug)
    assert last["slug"] == handle["session_slug"]
    assert not last["open"]
    summary = last["summary"]
    assert f"Closed tasks: {closed['slug']} (Finish up)" in summary
    assert f"Claimed tasks: {claimed['slug']} (Keep going) [in_progress]" in summary
    assert theirs["slug"] not in summary
    assert f"{mem['slug']} (Mind the gap)" in summary
    assert "Branch: feat/thing" in summary
    assert "feat: the work" in summary
    assert session_state.read_handle(SID) is None


@requires_omnigraph
def test_auto_close_of_an_idle_session_says_so(server, hook, monkeypatch, tmp_path):
    proj = server.workflow_project_create(title="idle", description="d")
    server.workflow_session_start(
        project_slug=proj["slug"], session_id=SID, phase="spec"
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

    hook.session_checkpoint()

    assert _handoff(server, proj["slug"])["summary"] == session_summary.NO_ACTIVITY


@requires_omnigraph
def test_failed_reads_still_close_the_session(server, hook, monkeypatch, tmp_path):
    """Every graph read fails; the close (a write) still lands, with the
    placeholder, and the handle is consumed."""
    from witan import server as srv
    from witan.cli import _common

    proj = server.workflow_project_create(title="flaky", description="d")
    handle = server.workflow_session_start(
        project_slug=proj["slug"], session_id=SID, phase="spec"
    )

    class _ReadsFail:
        def __getattr__(self, name):
            if name in {
                "workflow_session_list",
                "task_list",
                "workflow_project_memories",
            }:

                def _raise(*_a, **_kw):
                    raise RuntimeError(f"{name} timed out")

                return _raise
            return getattr(srv, name)

    monkeypatch.setattr(_common, "_server", _ReadsFail())
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

    hook.session_checkpoint()

    last = _handoff(server, proj["slug"])
    assert last["slug"] == handle["session_slug"]
    assert not last["open"]
    assert last["summary"] == session_summary.PLACEHOLDER
    assert session_state.read_handle(SID) is None


@requires_omnigraph
def test_an_explicit_summary_is_never_replaced(server, hook, monkeypatch, tmp_path):
    """An explicit end through a deployment leaves the client's handle parked
    (nothing on the client clears it). The next Stop must leave the agent's
    summary alone and only drop the stale handle."""
    proj = server.workflow_project_create(title="explicit", description="d")
    handle = server.workflow_session_start(
        project_slug=proj["slug"], session_id=SID, phase="spec"
    )
    server.workflow_session_end(handle["session_slug"], summary="Wrote it myself.")
    assert session_state.read_handle(SID) is None  # local stdio cleared it
    session_state.write_handle(SID, handle)  # what a deployment leaves behind
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    ended_at = _handoff(server, proj["slug"])["ended_at"]

    hook.session_checkpoint()

    last = _handoff(server, proj["slug"])
    assert last["summary"] == "Wrote it myself."
    assert last["ended_at"] == ended_at
    assert session_state.read_handle(SID) is None


@requires_omnigraph
def test_memories_can_be_read_for_one_session(server):
    proj = server.workflow_project_create(title="mems", description="d")
    mine = server.workflow_session_start(
        project_slug=proj["slug"], session_id=uuid.uuid4().hex, phase="spec"
    )
    other = server.workflow_session_start(
        project_slug=proj["slug"], session_id=uuid.uuid4().hex, phase="spec"
    )
    kept = server.memory_store(
        kind="lesson", title="mine", content="a", session_slug=mine["session_slug"]
    )
    server.memory_store(
        kind="lesson", title="theirs", content="b", session_slug=other["session_slug"]
    )

    result = server.workflow_project_memories(
        proj["slug"], session=mine["session_slug"]
    )

    assert [m["slug"] for m in result["memories"]] == [kept["slug"]]
    assert len(server.workflow_project_memories(proj["slug"])["memories"]) == 2

    elsewhere = server.workflow_project_create(title="elsewhere", description="d")
    mismatched = server.workflow_project_memories(
        elsewhere["slug"], session=mine["session_slug"]
    )
    assert mismatched["memories"] == []


@requires_omnigraph
def test_auto_close_survives_a_deleted_working_directory(
    server, hook, monkeypatch, tmp_path
):
    """`Path.cwd()` raises once the directory is gone; the close must still land."""
    proj = server.workflow_project_create(title="gone", description="d")
    handle = server.workflow_session_start(
        project_slug=proj["slug"], session_id=SID, phase="spec"
    )
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    monkeypatch.chdir(doomed)
    doomed.rmdir()

    hook.session_checkpoint()

    last = _handoff(server, proj["slug"])
    assert last["slug"] == handle["session_slug"]
    assert not last["open"]
    assert session_state.read_handle(SID) is None


def test_parse_timestamp_reads_naive_as_utc():
    naive = session_summary.parse_timestamp("2026-11-12T15:00:00.000")
    aware = session_summary.parse_timestamp("2026-11-12T10:00:00.000-05:00")
    assert naive == aware == SINCE
    assert session_summary.parse_timestamp("not a time") is None
    assert session_summary.parse_timestamp(None) is None
