import json
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP

from . import config as cfg_module
from . import repo as repo_module
from .graph import OmnigraphClient

# ── Startup ───────────────────────────────────────────────────────

cfg = cfg_module.load()
client = OmnigraphClient(cfg.graph_uri, cfg.queries_dir, cfg.graph_token)

mcp = FastMCP(
    "omnigraph-memory",
    instructions=(
        "Team-wide agent memory backed by Omnigraph. "
        "Stores and retrieves coding patterns, project facts, lessons, "
        "and agent context scoped to repositories."
    ),
)

# ── Helpers ───────────────────────────────────────────────────────

MemoryKind = Literal["pattern", "project_fact", "lesson", "agent_context"]

_KIND_PREFIX = {
    "pattern": "pat",
    "project_fact": "pf",
    "lesson": "les",
    "agent_context": "ctx",
    "workflow_project": "wp",
    "workflow_session": "ws",
    "workflow_trace": "wt",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_slug(kind: str, title: str) -> str:
    """Generate a stable, human-readable slug from kind and title."""
    prefix = _KIND_PREFIX.get(kind, "mem")
    sanitised = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48]
    short_id = uuid.uuid4().hex[:6]
    return f"{prefix}-{sanitised}-{short_id}"


# ── Tools ─────────────────────────────────────────────────────────


@mcp.tool
def memory_search(
    query: str,
    repo: str | None = None,
    kind: MemoryKind | None = None,
) -> list[dict]:
    """
    Search agent memories by text.

    Returns the top-20 matching memories ranked by BM25 relevance. The search
    is automatically scoped to the current git repository unless ``repo`` or
    ``OMNIGRAPH_MEMORY_REPO`` overrides it.

    Parameters
    ----------
    query:
        Free-text search query. Searched against ``content``.
    repo:
        Canonical repo slug (e.g. ``github.com/mitodl/ol-django``).
        Auto-detected from ``.git/config`` if omitted.
    kind:
        Optional filter: ``pattern``, ``project_fact``, ``lesson``,
        or ``agent_context``.
    """
    detected = repo_module.detect(override=repo)

    if detected and kind:
        return client.read(
            "read.gq",
            "search_by_repo_and_kind",
            {"query": query, "repo": detected, "kind": kind},
        )
    if detected:
        return client.read(
            "read.gq",
            "search_by_repo",
            {"query": query, "repo": detected},
        )
    if kind:
        return client.read(
            "read.gq",
            "search_by_kind",
            {"query": query, "kind": kind},
        )
    return client.read("read.gq", "search_all", {"query": query})


@mcp.tool
def memory_store(
    kind: MemoryKind,
    title: str,
    content: str,
    repo: str | None = None,
    language: str | None = None,
    category: str | None = None,
    severity: Literal["info", "warning", "critical"] | None = None,
    tags: list[str] | None = None,
) -> dict:
    """
    Store a new memory in the graph.

    Returns the slug of the created node so callers can link to it.

    Parameters
    ----------
    kind:
        ``pattern``      — coding convention or reusable technique
        ``project_fact`` — structural fact about a repo/service
        ``lesson``       — a correction or cautionary finding
        ``agent_context``— information a future agent on this task should know
    title:
        Short, human-readable label. Used in listings and search.
    content:
        Full text of the memory. Be specific: include the what, why, and any
        examples. This is the primary search target.
    repo:
        Canonical repo slug. Auto-detected from ``.git/config`` if omitted.
    language:
        Programming language (for ``pattern`` kind). e.g. ``python``, ``typescript``.
    category:
        Thematic category (for ``project_fact`` kind).
        e.g. ``architecture``, ``deployment``, ``testing``, ``dependencies``.
    severity:
        Importance level (for ``lesson`` kind).
        ``info`` | ``warning`` | ``critical``.
    tags:
        Optional list of free-form tags for grouping.
    """
    now = _now_iso()
    slug = _make_slug(kind, title)
    detected_repo = repo_module.detect(override=repo)

    client.change(
        "mutations.gq",
        "insert_memory",
        {
            "slug": slug,
            "kind": kind,
            "title": title,
            "content": content,
            "repo": detected_repo,
            "language": language,
            "category": category,
            "severity": severity,
            "author": cfg.author,
            "tags": tags,
            "createdAt": now,
            "updatedAt": now,
        },
    )
    return {"slug": slug, "kind": kind, "repo": detected_repo}


@mcp.tool
def memory_get(slug: str) -> dict | None:
    """
    Retrieve a single memory by its slug.

    Returns the full node or ``null`` if not found.
    """
    rows = client.read("read.gq", "get_memory", {"slug": slug})
    return rows[0] if rows else None


@mcp.tool
def memory_get_project_facts(repo: str | None = None) -> list[dict]:
    """
    Return all project facts for a repository.

    Use this at the start of a session in an unfamiliar codebase to load
    structural context: architecture, deployment topology, testing conventions,
    known dependencies and quirks.

    Parameters
    ----------
    repo:
        Canonical repo slug. Auto-detected from ``.git/config`` if omitted.
    """
    detected = repo_module.detect(override=repo)
    if not detected:
        return []
    return client.read("read.gq", "get_project_facts", {"repo": detected})


@mcp.tool
def memory_list_patterns(
    repo: str | None = None,
    language: str | None = None,
) -> list[dict]:
    """
    List coding patterns, optionally scoped to a repo and/or language.

    Use before writing code in a familiar service to check what conventions
    the team has documented. When both ``repo`` and ``language`` are provided,
    the server fetches by ``repo`` and post-filters by ``language`` in Python
    (avoiding combinatorial query variants).

    Parameters
    ----------
    repo:
        Canonical repo slug. Auto-detected from ``.git/config`` if omitted.
    language:
        Optional language filter applied after fetching. e.g. ``python``.
    """
    detected = repo_module.detect(override=repo)

    if detected:
        rows = client.read("read.gq", "patterns_by_repo", {"repo": detected})
    else:
        rows = client.read("read.gq", "patterns_all", {})

    if language:
        rows = [
            r for r in rows if (r.get("language") or "").lower() == language.lower()
        ]

    return rows


# ── Workflow Tracking Tools ───────────────────────────────────────

WorkflowPhase = Literal["discovery", "spec", "implementation", "delivery"]
WorkflowStatus = Literal["active", "completed", "abandoned"]

_STATE_FILE_PREFIX = "workflow-session-"


def _session_state_path(session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"{_STATE_FILE_PREFIX}{session_id}.json"


@mcp.tool
def workflow_project_create(
    title: str,
    description: str,
    phase: WorkflowPhase = "discovery",
    repo: str | None = None,
    github_issue: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """
    Create a new workflow project to track an engineering objective.

    Call this at the start of a multi-session project before calling
    ``workflow_session_start``. The returned slug is used to link sessions.

    Parameters
    ----------
    title:
        Short name for the project. Used in listings and injected context.
    description:
        Full description of the objective — what will be built or changed and why.
    phase:
        Starting phase. One of ``discovery``, ``spec``, ``implementation``,
        ``delivery``. Defaults to ``discovery``.
    repo:
        Canonical repo slug. Auto-detected from ``.git/config`` if omitted.
    github_issue:
        URL of the GitHub issue tracking this work.
        e.g. ``github.com/mitodl/ol-django/issues/847``.
    tags:
        Optional list of tags for grouping and searching.
    """
    now = _now_iso()
    slug = _make_slug("workflow_project", title)
    detected_repo = repo_module.detect(override=repo)

    client.change(
        "mutations.gq",
        "insert_workflow_project",
        {
            "slug": slug,
            "title": title,
            "description": description,
            "repo": detected_repo,
            "status": "active",
            "phase": phase,
            "author": cfg.author,
            "tags": tags,
            "githubIssue": github_issue,
            "githubPR": None,
            "createdAt": now,
            "updatedAt": now,
        },
    )
    return {"slug": slug, "repo": detected_repo, "phase": phase}


@mcp.tool
def workflow_project_get(slug: str) -> dict | None:
    """
    Retrieve a single workflow project by slug.

    Returns the full project node or ``null`` if not found.
    """
    rows = client.read("read.gq", "get_workflow_project", {"slug": slug})
    return rows[0] if rows else None


@mcp.tool
def workflow_project_list(
    repo: str | None = None,
    status: WorkflowStatus | None = "active",
    phase: WorkflowPhase | None = None,
) -> list[dict]:
    """
    List workflow projects, optionally filtered by repo, status, and phase.

    Defaults to listing only ``active`` projects. Pass ``status=None`` to see
    all statuses. The ``UserPromptSubmit`` hook calls this to inject project
    context into new sessions.

    Parameters
    ----------
    repo:
        Canonical repo slug. Auto-detected from ``.git/config`` if omitted.
        Pass an empty string to list projects across all repos.
    status:
        ``active`` | ``completed`` | ``abandoned`` | ``None`` for all.
        Defaults to ``active``.
    phase:
        Optional phase filter applied after fetching.
    """
    detected_repo = repo_module.detect(override=repo)

    if detected_repo and status:
        rows = client.read(
            "read.gq",
            "list_projects_by_repo_status",
            {"repo": detected_repo, "status": status},
        )
    elif detected_repo:
        rows = client.read(
            "read.gq",
            "list_projects_by_repo",
            {"repo": detected_repo},
        )
    elif status:
        rows = client.read(
            "read.gq",
            "list_projects_by_status",
            {"status": status},
        )
    else:
        rows = client.read("read.gq", "list_all_projects", {})

    if phase:
        rows = [r for r in rows if r.get("phase") == phase]

    return rows


@mcp.tool
def workflow_project_advance(
    slug: str,
    phase: WorkflowPhase,
    github_pr: str | None = None,
) -> dict:
    """
    Advance a workflow project to the next phase.

    Call when transitioning from e.g. spec to implementation. Optionally
    record a PR URL when moving to or through the delivery phase.

    Parameters
    ----------
    slug:
        The ``wp-`` slug of the project to update.
    phase:
        New phase: ``discovery`` | ``spec`` | ``implementation`` | ``delivery``.
    github_pr:
        URL of the GitHub PR if one has been opened.
    """
    now = _now_iso()
    client.change(
        "mutations.gq",
        "update_workflow_project_phase",
        {"slug": slug, "phase": phase, "githubPR": github_pr, "updatedAt": now},
    )
    rows = client.read("read.gq", "get_workflow_project", {"slug": slug})
    return rows[0] if rows else {"slug": slug, "phase": phase}


@mcp.tool
def workflow_project_complete(
    slug: str,
    outcome: str,
    github_pr: str | None = None,
) -> dict:
    """
    Mark a workflow project as completed and assemble its corpus trace.

    This creates a ``WorkflowTrace`` node that aggregates all linked sessions
    into an immutable record for later pattern mining. Idempotent: if a trace
    already exists for this project, it is returned without re-inserting.

    Parameters
    ----------
    slug:
        The ``wp-`` slug of the project to complete.
    outcome:
        Free-text narrative of what was delivered. Be specific — this is
        the primary content of the corpus record.
    github_pr:
        URL of the merged PR, if applicable.
    """
    now = _now_iso()

    # Idempotency: return existing trace if already completed
    trace_slug = f"wt-{slug}"
    existing = client.read("read.gq", "get_trace", {"slug": trace_slug})
    if existing:
        return {"project_slug": slug, "trace_slug": trace_slug, "existed": True}

    # Mark project completed
    client.change(
        "mutations.gq",
        "update_workflow_project_complete",
        {
            "slug": slug,
            "status": "completed",
            "githubPR": github_pr,
            "completedAt": now,
            "updatedAt": now,
        },
    )

    # Fetch project and all sessions to assemble trace
    project_rows = client.read("read.gq", "get_workflow_project", {"slug": slug})
    project = project_rows[0] if project_rows else {}

    sessions = client.read("read.gq", "list_sessions_by_project", {"projectSlug": slug})

    # Compute trace fields
    session_count = len(sessions)
    phases_seen: list[str] = []
    for s in sessions:
        p = s.get("phase")
        if p and p not in phases_seen:
            phases_seen.append(p)

    duration: int | None = None
    if sessions:
        started_vals = [s.get("startedAt") for s in sessions if s.get("startedAt")]
        ended_vals = [s.get("endedAt") for s in sessions if s.get("endedAt")]
        if started_vals and ended_vals:
            try:
                first = datetime.fromisoformat(min(started_vals))
                last = datetime.fromisoformat(max(ended_vals))
                duration = max(1, int((last - first).total_seconds() / 3600))
            except (ValueError, TypeError):
                pass

    client.change(
        "mutations.gq",
        "insert_workflow_trace",
        {
            "slug": trace_slug,
            "projectSlug": slug,
            "repo": project.get("repo"),
            "title": project.get("title", slug),
            "description": project.get("description", ""),
            "sessionCount": session_count,
            "phases": phases_seen,
            "duration": duration,
            "outcome": outcome,
            "lessonsSlug": None,
            "patternsSlug": None,
            "author": cfg.author,
            "tags": project.get("tags"),
            "createdAt": now,
        },
    )
    client.change(
        "mutations.gq",
        "link_produced",
        {"from": slug, "to": trace_slug},
    )

    return {"project_slug": slug, "trace_slug": trace_slug, "existed": False}


@mcp.tool
def workflow_session_start(
    project_slug: str,
    session_id: str,
    phase: WorkflowPhase,
    repo: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """
    Link the current Claude Code session to a workflow project.

    Call this at the start of any session that is contributing to a tracked
    project. The injected context from the ``UserPromptSubmit`` hook provides
    the ``project_slug``; ``session_id`` should be the Claude Code session UUID
    (available as the ``CLAUDE_SESSION_ID`` environment variable, or any stable
    unique string for the session if that variable is not set).

    Also writes a state file to ``/tmp`` so the ``Stop`` hook can close the
    session automatically if ``workflow_session_end`` is not called explicitly.

    Parameters
    ----------
    project_slug:
        The ``wp-`` slug of the project this session belongs to.
    session_id:
        Unique identifier for this Claude Code session.
    phase:
        The phase this session is working in.
    repo:
        Canonical repo slug. Auto-detected from ``.git/config`` if omitted.
    tags:
        Optional tags.
    """
    now = _now_iso()
    slug = _make_slug("workflow_session", project_slug)
    detected_repo = repo_module.detect(override=repo)

    client.change(
        "mutations.gq",
        "insert_workflow_session",
        {
            "slug": slug,
            "projectSlug": project_slug,
            "sessionId": session_id,
            "repo": detected_repo,
            "phase": phase,
            "summary": "",
            "author": cfg.author,
            "tags": tags,
            "startedAt": now,
        },
    )
    client.change(
        "mutations.gq",
        "link_belongs_to",
        {"from": slug, "to": project_slug},
    )

    # Write state file so Stop hook can close this session
    state = {"session_slug": slug, "project_slug": project_slug, "started_at": now}
    state_path = _session_state_path(session_id)
    try:
        state_path.write_text(json.dumps(state))
    except OSError:
        pass

    return {"session_slug": slug, "project_slug": project_slug, "phase": phase}


@mcp.tool
def workflow_session_end(
    session_slug: str,
    summary: str,
    tools_used: list[str] | None = None,
    files_changed: list[str] | None = None,
) -> dict:
    """
    Close the current session with a summary of work accomplished.

    Call this before ending a session to produce a high-quality corpus record.
    The ``Stop`` hook will auto-close sessions that did not call this, but
    with a placeholder summary.

    For best corpus quality, write a summary that includes:
    - What was done this session
    - What remains for the next session
    - Any blockers or decisions made

    Parameters
    ----------
    session_slug:
        The ``ws-`` slug returned by ``workflow_session_start``.
    summary:
        Description of what was accomplished and what remains.
    tools_used:
        List of Claude Code tool names used. e.g. ``["Edit", "Bash", "Read"]``.
    files_changed:
        List of file paths modified in this session.
    """
    now = _now_iso()
    client.change(
        "mutations.gq",
        "update_workflow_session_end",
        {
            "slug": session_slug,
            "summary": summary,
            "toolsUsed": tools_used,
            "filesChanged": files_changed,
            "endedAt": now,
        },
    )

    # Clean up state file for any session_id that maps to this slug
    # (best-effort; Stop hook will also attempt cleanup)
    tmp = Path(tempfile.gettempdir())
    for state_file in tmp.glob(f"{_STATE_FILE_PREFIX}*.json"):
        try:
            data = json.loads(state_file.read_text())
            if data.get("session_slug") == session_slug:
                state_file.unlink(missing_ok=True)
                break
        except (OSError, json.JSONDecodeError):
            continue

    return {"session_slug": session_slug, "ended_at": now}
