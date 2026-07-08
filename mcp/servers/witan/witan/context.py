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

# Each omnigraph read is a full store scan (~1-2s on a large graph), and the hook
# issues several per prompt — so a burst of prompts (e.g. picking a skill via a
# `/` command) can each pay multiple seconds and blow the hook timeout. Cache the
# *rendered* block on disk with a short TTL so only the first prompt in a window
# pays the cost; the rest read one small file. The content is advisory, so a few
# seconds of staleness is fine. Override with WITAN_CONTEXT_TTL (seconds; 0
# disables).
_OUTPUT_CACHE_TTL = 30.0


def _output_cache_ttl() -> float:
    raw = os.environ.get("WITAN_CONTEXT_TTL")
    if raw is None:
        return _OUTPUT_CACHE_TTL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _OUTPUT_CACHE_TTL


def _atomic_write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically and privately; never raises.

    The hook runs as concurrent fresh processes (a ``/``-command burst fires it
    repeatedly), so a plain ``write_text`` could let one process read a half-
    written file. Writing a process-unique temp file and ``os.replace``-ing it in
    means a reader always sees either the old or the new *complete* file. The
    temp file is created ``0600`` (umask-independent) because the cached block
    contains project/task titles and lives in a shared temp dir.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, text.encode())
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _output_cache_file(graph_uri: str, repo: str | None, branch: str | None) -> Path:
    key = f"{graph_uri}|{repo}|{branch}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return session_state.session_state_dir() / f"witan-ctx-{digest}.json"


def _read_output_cache(
    graph_uri: str, repo: str | None, branch: str | None
) -> str | None:
    ttl = _output_cache_ttl()
    if ttl <= 0:
        return None
    try:
        data = json.loads(_output_cache_file(graph_uri, repo, branch).read_text())
        if time.time() - data["stamp"] < ttl:
            return data["output"]
    except Exception:  # noqa: BLE001 — missing/corrupt/stale cache → recompute
        return None
    return None


def _write_output_cache(
    graph_uri: str, repo: str | None, branch: str | None, output: str
) -> None:
    if _output_cache_ttl() <= 0:
        return
    _atomic_write_private(
        _output_cache_file(graph_uri, repo, branch),
        json.dumps({"stamp": time.time(), "output": output}),
    )


# ── Context injection (UserPromptSubmit hook) ─────────────────────────────────


def inject_context(graph_uri: str, queries_dir: Path, token: str | None) -> str:
    """Return markdown context for active projects + ready tasks, or empty string.

    Builds the same output as ``workflow-context-inject.sh`` without the
    checkout-relative QUERIES_DIR assumption.
    """
    try:
        # Kept inside the try so the hook still degrades to "" on the off chance
        # any of this raises — the module contract is that it never does.
        repo, branch = _cached_repo_and_branch()

        # Serve a recently-rendered block without touching the graph at all.
        cached = _read_output_cache(graph_uri, repo, branch)
        if cached is not None:
            return cached

        client = OmnigraphClient(graph_uri, queries_dir, token)

        # One read returns every Task (the "list_unscoped_tasks" query is an
        # all-tasks scan). Derive both the unscoped and the repo-scoped sets from
        # it rather than issuing a second list_tasks_by_repo read — each omnigraph
        # read is ~1-2s of fixed scan overhead regardless of row count, so fewer
        # reads is the win, not narrower results.
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
            repo_tasks = [r for r in all_rows if r.get("repo") == repo]

        # Unscoped (no repo) and repo-scoped sets are disjoint; the dedup is just
        # belt-and-suspenders.
        seen = {t["slug"] for t in repo_tasks}
        tasks = repo_tasks + [t for t in unscoped if t["slug"] not in seen]

        # Every session in one read, grouped by project — one omnigraph call for
        # the resume/staleness lines instead of one per shown project.
        sessions_by_project: dict[str, list[dict]] = {}
        if projects:
            for s in client.read("read.gq", "list_all_sessions", {}):
                sessions_by_project.setdefault(s.get("project_slug"), []).append(s)
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
            lines.extend(
                _project_session_lines(sessions_by_project.get(p["slug"], []), p)
            )
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
        output = ""
    else:
        if projects:
            lines.append("If this is unrelated work, ignore the above.")
        output = "\n".join(lines)

    # Cache the freshly rendered block (including an empty one) so the next
    # prompt in this window skips the graph reads entirely.
    _write_output_cache(graph_uri, repo, branch, output)
    return output


# A project sitting through this many sessions in the same phase without
# advancing is a soft signal that it may be stuck — nudge, don't block.
_STALE_SESSION_THRESHOLD = 4


def _project_session_lines(sessions: list[dict], project: dict) -> list[str]:
    """Continuity + staleness lines for one project from its sessions.

    ``sessions`` is this project's slice of the single all-sessions read, ordered
    by ``started_at`` asc. Returns up to two lines: the latest session's handoff
    summary (the artifact written by ``workflow_session_end`` that is otherwise
    invisible on resume) and a staleness nudge when many sessions have accrued in
    the current phase without advancing. Pure and fault-tolerant: an empty list
    yields ``[]`` so a missing/failed sessions read can never blank the
    projects/tasks context that already works.
    """
    if not sessions:
        return []

    out: list[str] = []

    latest = sessions[-1]  # ordered by started_at asc → last is newest
    summary_lines = (latest.get("summary") or "").strip().splitlines()
    summary = summary_lines[0][:200] if summary_lines else ""
    if summary:
        state = "still open" if not latest.get("ended_at") else "ended"
        out.append(f"  Last session ({state}): {summary}")

    phase = project.get("phase")
    if phase:
        in_phase = sum(1 for s in sessions if s.get("phase") == phase)
        if in_phase >= _STALE_SESSION_THRESHOLD:
            out.append(
                f"  ⚠ {in_phase} sessions in `{phase}` — if this phase is done, "
                "call `workflow_project_advance` (or `workflow_project_complete`)."
            )
    return out


def _cached_repo_and_branch() -> tuple[str | None, str | None]:
    """``(repo, branch)`` for the current checkout, cached on disk with a short
    TTL so the prompt hook doesn't spawn git on every prompt.

    Fully fault-tolerant: a cache miss, unreadable/stale entry, or any detection
    error falls through to (or returns) live values and never raises. When
    ``WITAN_REPO`` is set, detection needs no git at all, so the cache is skipped.
    """
    project_dir = _cwd_or_dot()

    # WITAN_REPO short-circuits git — nothing to amortize, and it can differ from
    # what a dir-keyed cache holds, so don't consult/write the cache in that mode.
    # Skip branch detection when there's no repo (e.g. WITAN_REPO=""): a branch
    # is only used to join CodeBranch, so it's wasted git work without a repo.
    if os.environ.get("WITAN_REPO") is not None:
        repo = _detect_repo()
        return repo, (_current_branch() if repo else None)

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
    _atomic_write_private(
        cache_file, json.dumps({"stamp": time.time(), "repo": repo, "branch": branch})
    )
    return repo, branch


def _cwd_or_dot() -> str:
    """``$CLAUDE_PROJECT_DIR`` or the cwd, degrading to ``"."`` — ``Path.cwd()``
    itself raises ``OSError`` if the working directory was deleted."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        return project_dir
    try:
        return str(Path.cwd())
    except OSError:
        return "."


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

    project_dir = _cwd_or_dot()
    try:
        raw = subprocess.check_output(
            ["git", "-C", project_dir, "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        # OSError covers a missing git binary (FileNotFoundError) so this hook
        # helper degrades to "no repo" instead of crashing the prompt hook.
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
