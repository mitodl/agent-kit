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

    slug1 = srv._upsert_code_branch(REPO, "feature/touch")
    first = srv.client.read("read.gq", "get_code_branch", {"slug": slug1})[0]

    slug2 = srv._upsert_code_branch(REPO, "feature/touch")
    second = srv.client.read("read.gq", "get_code_branch", {"slug": slug2})

    assert slug1 == slug2
    assert len(second) == 1, "touching an existing branch must not insert a duplicate"
    assert second[0]["updated_at"] >= first["updated_at"]
