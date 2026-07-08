"""Logic for the inject-context and session-checkpoint CLI commands.

Both commands are invoked as Claude Code hooks and must be completely
fault-tolerant — they never raise, never block, and never produce unexpected
output. Porting these from shell scripts into Python lets the hooks be
one-liners (``witan inject-context`` / ``witan session-checkpoint``) that
work regardless of whether witan was installed from a checkout or via uvx.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import readiness
from . import repo as repo_module
from . import session_state
from .graph import OmnigraphClient

# The context hook runs a fresh process on every prompt, so an in-process cache
# (like witan-code's server-side ``_cached_git``) can't help it. A tiny on-disk
# cache keyed by project dir amortizes the git subprocesses across a burst of
# prompts instead. Short TTL so a branch switch is picked up almost immediately.
_REPO_CACHE_TTL = 5.0


# ── Context injection (UserPromptSubmit hook) ─────────────────────────────────


def inject_context(graph_uri: str, queries_dir: Path, token: str | None) -> str:
    """Return markdown context for active projects + ready tasks, or empty string.

    Builds the same output as ``workflow-context-inject.sh`` without the
    checkout-relative QUERIES_DIR assumption.
    """
    repo, branch = _cached_repo_and_branch()
    try:
        client = OmnigraphClient(graph_uri, queries_dir, token)

        # Unscoped tasks (no repo) are always relevant regardless of cwd.
        all_rows = client.read("read.gq", "list_unscoped_tasks", {})
        unscoped = [r for r in all_rows if not r.get("repo")]

        projects: list[dict] = []
        repo_tasks: list[dict] = []
        if repo:
            projects = client.read(
                "read.gq",
                "list_projects_by_status",
                {"status": "active"},
            )
            projects = [p for p in projects if repo in (p.get("repos") or [])]
            repo_tasks = client.read("read.gq", "list_tasks_by_repo", {"repo": repo})

        # Merge, deduplicating unscoped tasks already returned by repo query.
        seen = {t["slug"] for t in repo_tasks}
        tasks = repo_tasks + [t for t in unscoped if t["slug"] not in seen]
    except Exception:  # noqa: BLE001
        return ""

    # Isolated from the block above: a CodeBranch query failing (e.g. an
    # existing store that hasn't run `witan migrate schema` since CodeBranch
    # was added) must never blank the projects/ready-tasks context that
    # already works.
    branch_tasks: list[dict] = []
    if repo and branch:
        try:
            branch_tasks = client.read(
                "read.gq",
                "code_branch_tasks",
                {"branch_slug": f"{repo}|{branch}"},
            )
        except Exception:  # noqa: BLE001
            branch_tasks = []
    open_branch_tasks = [t for t in branch_tasks if t.get("status") != "closed"]

    # Shared with ``task_ready`` so the injected list and the tool agree —
    # including the reclaim of ``in_progress`` tasks whose lease has lapsed.
    ready = readiness.filter_ready(tasks)

    lines: list[str] = []

    if projects:
        proj_header = f"This repository has {len(projects)} active tracked project(s)"
        if len(projects) > 3:
            proj_header += " — showing the first 3, run `witan projects` for all"
        lines += [
            "## Active Workflow Projects",
            "",
            f"{proj_header}:",
            "",
        ]
        for p in projects[:3]:
            lines.append(f"- **{p['title']}** (slug: `{p['slug']}`)")
            lines.append(f"  Phase: {p['phase']}")
            if p.get("github_issue"):
                lines.append(f"  Issue: {p['github_issue']}")
            lines.extend(_project_session_lines(client, p))
        lines += [
            "",
            "If this session is contributing to one of the projects above, call",
            "`workflow_session_start` with the matching slug and the current phase",
            "before doing substantive work.",
            "",
        ]

    if open_branch_tasks:
        lines += [
            "## In-Flight Branch",
            "",
            "The current git branch is already linked to task(s) in progress:",
            "",
        ]
        for t in open_branch_tasks:
            held_by = f" (claimed by {t['assignee']})" if t.get("assignee") else ""
            lines.append(f"- **{t['title']}** (slug: `{t['slug']}`){held_by}")
        lines += [
            "",
            "This is likely the work this session should continue, not a new task.",
            "",
        ]

    if ready:
        ready_header = f"{len(ready)} task(s) are ready to work (no open blockers)"
        if len(ready) > 5:
            ready_header += (
                " — showing the top 5 by priority, run `witan tasks` for all"
            )
        lines += [
            "## Ready Tasks",
            "",
            f"{ready_header}:",
            "",
        ]
        for t in ready[:5]:
            ext = f" · {t['external_uri']}" if t.get("external_uri") else ""
            lines.append(
                f"- `[{t.get('priority', 'p2')}]` **{t['title']}**"
                f" (slug: `{t['slug']}`){ext}"
            )
        lines += [
            "",
            "Use `task_update`/`task_close` (or the `/witan-task` skill) to claim and progress them.",
            "",
        ]

    if not lines:
        return ""
    if projects:
        lines.append("If this is unrelated work, ignore the above.")
    return "\n".join(lines)


# A project sitting through this many sessions in the same phase without
# advancing is a soft signal that it may be stuck — nudge, don't block.
_STALE_SESSION_THRESHOLD = 4


def _project_session_lines(client: OmnigraphClient, project: dict) -> list[str]:
    """Continuity + staleness lines for one project, from a single sessions read.

    Returns up to two lines: the latest session's handoff summary (the artifact
    written by ``workflow_session_end`` that is otherwise invisible on resume)
    and a staleness nudge when many sessions have accrued in the current phase
    without advancing. Isolated and fault-tolerant: any failure (including a
    store without the session query) yields ``[]`` so a broken session read can
    never blank the projects/tasks context that already works.
    """
    try:
        sessions = client.read(
            "read.gq", "list_sessions_by_project", {"project_slug": project["slug"]}
        )
    except Exception:  # noqa: BLE001
        return []
    if not sessions:
        return []

    out: list[str] = []

    latest = sessions[-1]  # query orders by started_at asc → last is newest
    summary_lines = (latest.get("summary") or "").strip().splitlines()
    summary = summary_lines[0][:200] if summary_lines else ""
    if summary:
        state = "still open" if not latest.get("ended_at") else "ended"
        out.append(f"  Last session ({state}): {summary}")

    phase = project.get("phase")
    in_phase = sum(1 for s in sessions if s.get("phase") == phase)
    if in_phase >= _STALE_SESSION_THRESHOLD:
        out.append(
            f"  ⚠ {in_phase} sessions in `{phase}` — if this phase is done, call "
            "`workflow_project_advance` (or `workflow_project_complete`)."
        )
    return out


def _cached_repo_and_branch() -> tuple[str | None, str | None]:
    """``(repo, branch)`` for the current checkout, cached on disk with a short
    TTL so the prompt hook doesn't spawn git on every prompt.

    Fully fault-tolerant: a cache miss, unreadable/stale entry, or any detection
    error falls through to (or returns) live values and never raises. When
    ``WITAN_REPO`` is set, detection needs no git at all, so the cache is skipped.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())

    # WITAN_REPO short-circuits git — nothing to amortize, and it can differ from
    # what a dir-keyed cache holds, so don't consult/write the cache in that mode.
    if os.environ.get("WITAN_REPO") is not None:
        return _detect_repo(), _current_branch()

    digest = hashlib.sha1(project_dir.encode()).hexdigest()[:16]
    cache_file = session_state.session_state_dir() / f"witan-repo-{digest}.json"
    try:
        data = json.loads(cache_file.read_text())
        if time.time() - data["stamp"] < _REPO_CACHE_TTL:
            return data.get("repo"), data.get("branch")
    except Exception:  # noqa: BLE001
        pass

    repo = _detect_repo()
    branch = _current_branch() if repo else None
    try:
        cache_file.write_text(
            json.dumps({"stamp": time.time(), "repo": repo, "branch": branch})
        )
    except OSError:
        pass
    return repo, branch


def _current_branch() -> str | None:
    try:
        return repo_module.current_branch()
    except Exception:  # noqa: BLE001
        return None


def _detect_repo() -> str | None:
    """Detect canonical repo URI from WITAN_REPO, CLAUDE_PROJECT_DIR, or cwd.

    WITAN_REPO="" (explicitly set to empty string) suppresses detection entirely.
    """
    import re

    witan_repo = os.environ.get("WITAN_REPO")
    if witan_repo is not None:
        return witan_repo or None  # "" → disabled; non-empty → use as-is

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())
    try:
        raw = subprocess.check_output(
            ["git", "-C", project_dir, "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return None
    if not raw:
        return None
    url = re.sub(r"\.git$", "", raw).rstrip("/")
    if m := re.match(r"(?:ssh://)?[^@]+@([^:/]+)[:/](.+)", url):
        return f"https://{m.group(1)}/{m.group(2)}"
    if m := re.match(r"https?://(?:[^@/]+@)?([^/]+)/(.+)", url):
        return f"https://{m.group(1)}/{m.group(2)}"
    return url


# ── Session checkpoint (Stop hook) ────────────────────────────────────────────


def session_checkpoint(graph_uri: str, queries_dir: Path, token: str | None) -> None:
    """Auto-close the active WorkflowSession when the agent stops.

    Reads the state file written by ``workflow_session_start``. No-op when
    the file is absent (session was already closed explicitly).
    """
    session_id = os.environ.get("CLAUDE_SESSION_ID", "")
    if not session_id:
        return

    state_file = session_state.session_state_path(session_id)
    if not state_file.exists():
        return

    try:
        state = json.loads(state_file.read_text())
        session_slug = state.get("session_slug", "")
        if not session_slug:
            state_file.unlink(missing_ok=True)
            return

        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", str(Path.cwd()))
        try:
            changed = subprocess.check_output(
                ["git", "-C", project_dir, "diff", "--name-only", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).splitlines()[:50]
        except subprocess.CalledProcessError:
            changed = []

        client = OmnigraphClient(graph_uri, queries_dir, token)
        client.change(
            "mutations.gq",
            "update_workflow_session_end",
            {
                "slug": session_slug,
                "summary": (
                    "Session ended (auto-closed by Stop hook — "
                    "call workflow_session_end explicitly for a better summary)"
                ),
                "tools_used": None,
                "files_changed": changed or None,
                "ended_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:  # noqa: BLE001
        pass
    finally:
        state_file.unlink(missing_ok=True)
