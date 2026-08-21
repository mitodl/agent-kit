"""Tests for CodeBranch tracking: linking git branches to tasks/projects
(schema.pg § Code Branches)."""

import subprocess
import uuid

from .conftest import requires_omnigraph

REPO = "https://github.com/test/repo"


def _git(base, *args):
    subprocess.run(
        ["git", "-C", str(base), *args], check=True, capture_output=True, text=True
    )


def _git_repo(path):
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(
        path,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "init",
    )
    return path


@requires_omnigraph
def test_task_claim_creates_code_branch_and_works_on_edge(
    server, tmp_path, monkeypatch
):
    from witan import server as srv

    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/thing")
    monkeypatch.chdir(base)

    task = server.task_create(title="do the thing", description="x")
    claimed = server.task_claim(task["slug"])
    assert claimed["claimed"] is True

    branch_slug = f"{REPO}|feature/thing"
    branch = srv.client.read("read.gq", "get_code_branch", {"slug": branch_slug})
    assert branch and branch[0]["status"] == "active"

    linked_tasks = srv.client.read(
        "read.gq", "code_branch_tasks", {"branch_slug": branch_slug}
    )
    assert {t["slug"] for t in linked_tasks} == {task["slug"]}

    task_branches = srv.client.read(
        "read.gq", "task_code_branches", {"task_slug": task["slug"]}
    )
    assert {b["slug"] for b in task_branches} == {branch_slug}


@requires_omnigraph
def test_task_claim_renewal_does_not_duplicate_works_on_edge(
    server, tmp_path, monkeypatch
):
    from witan import server as srv

    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/renew")
    monkeypatch.chdir(base)

    task = server.task_create(title="renewed task", description="x")
    server.task_claim(task["slug"])
    server.task_claim(task["slug"])  # lease renewal — same branch, same task

    branch_slug = f"{REPO}|feature/renew"
    linked_tasks = srv.client.read(
        "read.gq", "code_branch_tasks", {"branch_slug": branch_slug}
    )
    assert len(linked_tasks) == 1, (
        "renewing a claim must not duplicate the WorksOn edge"
    )


@requires_omnigraph
def test_workflow_session_start_creates_code_branch_and_for_project_edge(
    server, tmp_path, monkeypatch
):
    from witan import server as srv

    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/proj-work")
    monkeypatch.chdir(base)

    proj = server.workflow_project_create(title="P", description="d", phase="spec")
    server.workflow_session_start(
        project_slug=proj["slug"], session_id=uuid.uuid4().hex, phase="spec"
    )

    branch_slug = f"{REPO}|feature/proj-work"
    branch = srv.client.read("read.gq", "get_code_branch", {"slug": branch_slug})
    assert (
        branch
        and branch[0]["repo"] == REPO
        and branch[0]["branch"] == "feature/proj-work"
    )

    edge = srv.client.read(
        "read.gq",
        "code_branch_for_project_edge",
        {"branch_slug": branch_slug, "project_slug": proj["slug"]},
    )
    assert edge


@requires_omnigraph
def test_task_claim_outside_git_does_not_create_code_branch(
    server, tmp_path, monkeypatch
):
    """No git context (a plain directory, no .git) — best-effort no-op, and
    the claim itself must still succeed."""
    from witan import server as srv

    empty_dir = tmp_path / "no-git"
    empty_dir.mkdir()
    monkeypatch.chdir(empty_dir)

    task = server.task_create(title="no branch context", description="x")
    claimed = server.task_claim(task["slug"])
    assert claimed["claimed"] is True

    all_branches = srv.client.read("read.gq", "code_branches_by_repo", {"repo": REPO})
    assert all_branches == []


@requires_omnigraph
def test_upsert_code_branch_touches_existing_branch(server, tmp_path, monkeypatch):
    from witan import server as srv

    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/touch")
    monkeypatch.chdir(base)

    # The step builder decides insert-vs-touch and the caller commits it, so
    # the round trip is build-then-issue rather than one call.
    srv.client.change_many([srv._upsert_code_branch_step(REPO, "feature/touch")])
    slug = srv._code_branch_slug(REPO, "feature/touch")
    first = srv.client.read("read.gq", "get_code_branch", {"slug": slug})[0]

    srv.client.change_many([srv._upsert_code_branch_step(REPO, "feature/touch")])
    second = srv.client.read("read.gq", "get_code_branch", {"slug": slug})

    assert len(second) == 1, "touching an existing branch must not insert a duplicate"
    assert second[0]["updated_at"] >= first["updated_at"]


@requires_omnigraph
def test_task_for_branch_returns_the_linked_task(server, tmp_path, monkeypatch):
    """The tool behind the ``## In-Flight Branch`` block on a deployed target.

    ``inject_context_remote`` cannot issue the ``code_branch_tasks`` read the
    local path uses — a deployment exposes tools, not queries — so this is the
    only route by which a shared graph can tell a session its branch is already
    spoken for (tk-the-in-flight-branch-anti-duplicate-work-signal--073f96).
    """
    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "feature/for-branch")
    monkeypatch.chdir(base)

    task = server.task_create(title="branch work", description="x")
    server.task_claim(task["slug"])

    linked = server.task_for_branch(branch="feature/for-branch")
    assert {t["slug"] for t in linked} == {task["slug"]}
    assert linked[0]["title"] == "branch work"
    # The render block prints "(claimed by ...)" off this field, which is what
    # makes the warning actionable rather than just noisy on a shared graph.
    assert linked[0]["assignee"]


@requires_omnigraph
def test_task_for_branch_is_scoped_to_the_repo(server, tmp_path, monkeypatch):
    """A branch name says nothing on its own — ``main`` and ``develop`` exist
    in every repo. The CodeBranch slug is ``repo|branch``, so an ignored
    ``repo`` would silently merge two repos' branches into one warning."""
    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "shared/name")
    monkeypatch.chdir(base)

    task = server.task_create(title="repo A work", description="x")
    server.task_claim(task["slug"])

    assert {t["slug"] for t in server.task_for_branch(branch="shared/name")} == {
        task["slug"]
    }
    assert (
        server.task_for_branch(
            branch="shared/name", repo="https://github.com/test/other"
        )
        == []
    )


@requires_omnigraph
def test_task_for_branch_unknown_branch_is_empty_not_an_error(
    server, tmp_path, monkeypatch
):
    """Every session on an unlinked branch takes this path, so it has to be a
    quiet empty rather than something the hook has to catch."""
    base = _git_repo(tmp_path / "r")
    monkeypatch.chdir(base)

    assert server.task_for_branch(branch=f"never-checked-out-{uuid.uuid4()}") == []
    assert server.task_for_branch(branch="") == []


# ── The deployed shape: a server with no checkout of its own ──────────────────
#
# Every other test in this file runs the server INSIDE a git repo, because under
# local stdio it really is a child of the agent and shares its working directory.
# A deployed server is not: it runs in a container with no checkout, so anything
# it tries to read from the filesystem comes back empty. These two pin the case
# the rest of the file structurally cannot reach — and both fail before
# tk-task-claim-never-writes-the-codebranch-workson-e-9496c4, where `branch` was
# read server-side and the edge was silently never written.


def _no_checkout(tmp_path, monkeypatch):
    """Put the server somewhere with no git repo above it, like a container."""
    empty = tmp_path / "container-with-no-checkout"
    empty.mkdir()
    monkeypatch.chdir(empty)
    return empty


@requires_omnigraph
def test_task_claim_tracks_the_branch_the_caller_names_with_no_checkout(
    server, tmp_path, monkeypatch
):
    """The regression. With no checkout to detect from, the caller's repo and
    branch have to arrive as arguments or the WorksOn edge is never written —
    which is what made `task_for_branch` answer zero for a branch that had just
    been claimed on, against a real deployment."""
    _no_checkout(tmp_path, monkeypatch)

    task = server.task_create(title="claimed from a laptop", description="x")
    claimed = server.task_claim(
        task["slug"], repo=REPO, branch="feature/from-the-client"
    )
    assert claimed["claimed"] is True

    linked = server.task_for_branch(branch="feature/from-the-client", repo=REPO)
    assert {t["slug"] for t in linked} == {task["slug"]}


@requires_omnigraph
def test_workflow_session_start_tracks_the_branch_the_caller_names(
    server, tmp_path, monkeypatch
):
    """Same defect on the ForProject half. `workflow_session_start` already took
    `repo` as a parameter, so only `branch` was being read off the server's own
    filesystem — enough on its own to skip the whole CodeBranch."""
    from witan import server as srv

    _no_checkout(tmp_path, monkeypatch)

    project = server.workflow_project_create(
        title="deployed session", description="x", repos=[REPO]
    )
    server.workflow_session_start(
        project_slug=project["slug"],
        session_id=f"sess-{uuid.uuid4()}",
        phase="implementation",
        repo=REPO,
        branch="feature/session-from-the-client",
    )

    slug = srv._code_branch_slug(REPO, "feature/session-from-the-client")
    assert srv.client.read("read.gq", "get_code_branch", {"slug": slug})
    assert srv.client.read(
        "read.gq",
        "code_branch_for_project_edge",
        {"branch_slug": slug, "project_slug": project["slug"]},
    )


@requires_omnigraph
def test_an_explicit_branch_wins_over_the_servers_own_checkout(
    server, tmp_path, monkeypatch
):
    """The fallback must not override what the caller said. Under local stdio the
    two agree; the case that matters is a client on branch A talking to a server
    that happens to sit in a checkout on branch B — only the client's answer is
    about the work being claimed."""
    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "server-side-branch")
    monkeypatch.chdir(base)

    task = server.task_create(title="two branches in play", description="x")
    server.task_claim(task["slug"], repo=REPO, branch="callers-branch")

    assert {t["slug"] for t in server.task_for_branch(branch="callers-branch")} == {
        task["slug"]
    }
    assert server.task_for_branch(branch="server-side-branch") == []


@requires_omnigraph
def test_an_explicit_empty_branch_means_do_not_track(server, tmp_path, monkeypatch):
    """`branch=""` is the "no branch" sentinel, matching `repo=""`, and must not
    fall back to the server's own checkout.

    `_map_args` passes an explicitly-empty branch through untouched — only an
    *omitted* one is filled in client-side — so a truthiness fallback here would
    build a CodeBranch out of whatever the server happens to be sitting in. On a
    deployment that is nothing and the bug hides; under local stdio it is the
    caller's own checkout, so it silently succeeds at the thing the caller asked
    not to happen. Hence the server-side git repo below: without it this test
    passes either way.
    """
    from witan import server as srv

    base = _git_repo(tmp_path / "r")
    _git(base, "checkout", "-q", "-b", "server-side-only")
    monkeypatch.chdir(base)

    task = server.task_create(title="untracked on purpose", description="x")
    assert server.task_claim(task["slug"], repo=REPO, branch="")["claimed"] is True

    assert srv.client.read("read.gq", "code_branches_by_repo", {"repo": REPO}) == []
