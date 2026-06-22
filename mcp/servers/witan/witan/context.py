"""Logic for the inject-context and session-checkpoint CLI commands.

Both commands are invoked as Claude Code hooks and must be completely
fault-tolerant — they never raise, never block, and never produce unexpected
output. Porting these from shell scripts into Python lets the hooks be
one-liners (``witan inject-context`` / ``witan session-checkpoint``) that
work regardless of whether witan was installed from a checkout or via uvx.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .graph import OmnigraphClient

_PRIORITY = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}


# ── Context injection (UserPromptSubmit hook) ─────────────────────────────────


def inject_context(graph_uri: str, queries_dir: Path, token: str | None) -> str:
    """Return markdown context for active projects + ready tasks, or empty string.

    Builds the same output as ``workflow-context-inject.sh`` without the
    checkout-relative QUERIES_DIR assumption.
    """
    try:
        client = OmnigraphClient(graph_uri, queries_dir, token)
        repo = _detect_repo()
        if not repo:
            return ""
        projects = client.read(
            "read.gq",
            "list_projects_by_status",
            {"status": "active"},
        )
        tasks = client.read("read.gq", "list_tasks_by_repo", {"repo": repo})
    except Exception:  # noqa: BLE001
        return ""

    projects = [p for p in projects if repo in (p.get("repos") or [])]

    status_by_slug = {t["slug"]: t.get("status") for t in tasks}
    ready = [
        t
        for t in tasks
        if t.get("status") in ("open", "blocked")
        and all(
            status_by_slug.get(b, "closed") == "closed"
            for b in (t.get("blocked_by") or [])
        )
    ]
    ready.sort(key=lambda t: _PRIORITY.get(t.get("priority", "p3"), 9))

    lines: list[str] = []

    if projects:
        lines += [
            "## Active Workflow Projects",
            "",
            f"This repository has {len(projects)} active tracked project(s):",
            "",
        ]
        for p in projects[:3]:
            lines.append(f"- **{p['title']}** (slug: `{p['slug']}`)")
            lines.append(f"  Phase: {p['phase']}")
            if p.get("github_issue"):
                lines.append(f"  Issue: {p['github_issue']}")
        lines += [
            "",
            "If this session is contributing to one of the projects above, call",
            "`workflow_session_start` with the matching slug and the current phase",
            "before doing substantive work.",
            "",
        ]

    if ready:
        lines += [
            "## Ready Tasks",
            "",
            f"{len(ready)} task(s) are ready to work (no open blockers):",
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
            "Use `task_update`/`task_close` (or the `/task` skill) to claim and progress them.",
            "",
        ]

    if not lines:
        return ""
    if projects:
        lines.append("If this is unrelated work, ignore the above.")
    return "\n".join(lines)


def _detect_repo() -> str | None:
    """Detect canonical repo URI from CLAUDE_PROJECT_DIR or cwd."""
    import re

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

    state_file = (
        Path(os.environ.get("TMPDIR", "/tmp")) / f"workflow-session-{session_id}.json"
    )
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
