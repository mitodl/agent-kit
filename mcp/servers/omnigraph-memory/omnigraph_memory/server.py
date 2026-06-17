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
    "task": "tk",
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
        Canonical repo URI (e.g. ``https://github.com/mitodl/ol-django``).
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
    symbol_refs: list[str] | None = None,
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
        Canonical repo URI. Auto-detected from ``.git/config`` if omitted.
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
    symbol_refs:
        Optional code-graph symbol ids (``repo#path::Name``) this memory concerns,
        e.g. the function a lesson is about. Resolved against the omnigraph-codegraph
        store; stored as a soft reference (no hard cross-store edge).
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
            "symbol_refs": symbol_refs,
            "created_at": now,
            "updated_at": now,
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
        Canonical repo URI. Auto-detected from ``.git/config`` if omitted.
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
        Canonical repo URI. Auto-detected from ``.git/config`` if omitted.
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
        Canonical repo URI. Auto-detected from ``.git/config`` if omitted.
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
            "github_issue": github_issue,
            "github_pr": None,
            "created_at": now,
            "updated_at": now,
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
        Canonical repo URI. Auto-detected from ``.git/config`` if omitted.
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
        {"slug": slug, "phase": phase, "github_pr": github_pr, "updated_at": now},
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
            "github_pr": github_pr,
            "completed_at": now,
            "updated_at": now,
        },
    )

    # Fetch project and all sessions to assemble trace
    project_rows = client.read("read.gq", "get_workflow_project", {"slug": slug})
    project = project_rows[0] if project_rows else {}

    sessions = client.read(
        "read.gq", "list_sessions_by_project", {"project_slug": slug}
    )

    # Compute trace fields
    session_count = len(sessions)
    phases_seen: list[str] = []
    for s in sessions:
        p = s.get("phase")
        if p and p not in phases_seen:
            phases_seen.append(p)

    duration: int | None = None
    if sessions:
        started_vals = [s.get("started_at") for s in sessions if s.get("started_at")]
        ended_vals = [s.get("ended_at") for s in sessions if s.get("ended_at")]
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
            "project_slug": slug,
            "repo": project.get("repo"),
            "title": project.get("title", slug),
            "description": project.get("description", ""),
            "session_count": session_count,
            "phases": phases_seen,
            "duration": duration,
            "outcome": outcome,
            "lessons_slug": None,
            "patterns_slug": None,
            "author": cfg.author,
            "tags": project.get("tags"),
            "created_at": now,
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
        Canonical repo URI. Auto-detected from ``.git/config`` if omitted.
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
            "project_slug": project_slug,
            "session_id": session_id,
            "repo": detected_repo,
            "phase": phase,
            "summary": "",
            "author": cfg.author,
            "tags": tags,
            "started_at": now,
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
            "tools_used": tools_used,
            "files_changed": files_changed,
            "ended_at": now,
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


# ── Task Tracking Tools ───────────────────────────────────────────
#
# A dependency-aware tracker living in the same graph as memory and workflow.
# Tasks are hierarchical (epic → sub-issue via `parent`) and can block one
# another; `task_ready` surfaces open tasks whose blockers are all closed.

TaskType = Literal["bug", "feature", "task", "chore", "epic"]
TaskStatus = Literal["open", "in_progress", "blocked", "closed"]
TaskPriority = Literal["p0", "p1", "p2", "p3"]
TaskLinkKind = Literal["blocks", "parent", "discovered_from", "addresses"]

_PRIORITY_ORDER = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}


def _unblock_dependents(repo: str | None) -> None:
    """Flip ``blocked`` tasks back to ``open`` once all their blockers are closed.

    Called after a task closes so ``task_list`` status stays truthful. Scoped to the
    closed task's repo (blockers and dependents share a repo in practice).
    """
    rows = (
        client.read("read.gq", "list_tasks_by_repo", {"repo": repo})
        if repo
        else client.read("read.gq", "list_all_tasks", {})
    )
    status_by_slug = {r["slug"]: r.get("status") for r in rows}

    def is_closed(blocker_slug: str) -> bool:
        if blocker_slug in status_by_slug:
            return status_by_slug[blocker_slug] == "closed"
        fetched = client.read("read.gq", "get_task", {"slug": blocker_slug})
        return fetched[0].get("status") == "closed" if fetched else True

    for r in rows:
        blockers = r.get("blocked_by") or []
        if (
            r.get("status") == "blocked"
            and blockers
            and all(is_closed(b) for b in blockers)
        ):
            _update_task(r["slug"], {"status": "open"})


def _update_task(slug: str, changes: dict) -> dict | None:
    """Read a task, merge ``changes`` over its mutable fields, write it back.

    Mirrors the read-merge-write pattern documented for ``update_memory`` so we
    avoid per-field update queries. Returns the updated node or ``None``.
    """
    rows = client.read("read.gq", "get_task", {"slug": slug})
    if not rows:
        return None
    current = rows[0]
    merged = {
        "slug": slug,
        "title": changes.get("title", current.get("title")),
        "description": changes.get("description", current.get("description")),
        "type": changes.get("type", current.get("type")),
        "status": changes.get("status", current.get("status")),
        "priority": changes.get("priority", current.get("priority")),
        "project_slug": changes.get("project_slug", current.get("project_slug")),
        "parent_slug": changes.get("parent_slug", current.get("parent_slug")),
        "blocked_by": changes.get("blocked_by", current.get("blocked_by")),
        "assignee": changes.get("assignee", current.get("assignee")),
        "external_uri": changes.get("external_uri", current.get("external_uri")),
        "resolution": changes.get("resolution", current.get("resolution")),
        "symbol_refs": changes.get("symbol_refs", current.get("symbol_refs")),
        "tags": changes.get("tags", current.get("tags")),
        "closed_at": changes.get("closed_at", current.get("closed_at")),
        "updated_at": _now_iso(),
    }
    client.change("mutations.gq", "update_task", merged)
    return client.read("read.gq", "get_task", {"slug": slug})[0]


@mcp.tool
def task_create(
    title: str,
    description: str,
    type: TaskType = "task",
    priority: TaskPriority = "p2",
    repo: str | None = None,
    project_slug: str | None = None,
    parent: str | None = None,
    blocked_by: list[str] | None = None,
    discovered_from: list[str] | None = None,
    external_uri: str | None = None,
    symbol_refs: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict:
    """
    Create a task in the work-coordination graph.

    Tasks are dependency-aware and hierarchical. Use ``parent`` to attach a
    sub-issue to an ``epic`` (or any parent task); use ``blocked_by`` to record
    dependencies so ``task_ready`` can withhold the task until its blockers
    close.

    Parameters
    ----------
    title, description:
        Short label and full text of the work.
    type:
        ``bug`` | ``feature`` | ``task`` | ``chore`` | ``epic``.
    priority:
        ``p0`` (highest) … ``p3``. Drives ``task_ready`` ordering.
    repo:
        Canonical repo URI. Auto-detected from ``.git/config`` if omitted.
    project_slug:
        ``wp-`` slug of the WorkflowProject this task rolls up to.
    parent:
        ``tk-`` slug of the parent task/epic. Sets the hierarchy edge.
    blocked_by:
        ``tk-`` slugs that must close before this task is ready.
    discovered_from:
        ``tk-`` slugs of tasks during which this work was discovered.
    external_uri:
        A reference URI — e.g. a GitHub issue or PR.
    symbol_refs:
        Code-graph symbol ids (``repo#path::Name``) this task concerns.
    tags:
        Optional free-form tags.
    """
    now = _now_iso()
    slug = _make_slug("task", title)
    detected_repo = repo_module.detect(override=repo)
    status: TaskStatus = "blocked" if blocked_by else "open"

    client.change(
        "mutations.gq",
        "insert_task",
        {
            "slug": slug,
            "title": title,
            "description": description,
            "repo": detected_repo,
            "type": type,
            "status": status,
            "priority": priority,
            "project_slug": project_slug,
            "parent_slug": parent,
            "blocked_by": blocked_by,
            "assignee": None,
            "external_uri": external_uri,
            "author": cfg.author,
            "symbol_refs": symbol_refs,
            "tags": tags,
            "created_at": now,
            "updated_at": now,
        },
    )

    if project_slug:
        client.change(
            "mutations.gq", "link_task_belongs_to", {"from": slug, "to": project_slug}
        )
    if parent:
        client.change("mutations.gq", "link_parent_of", {"from": parent, "to": slug})
    for blocker in blocked_by or []:
        client.change("mutations.gq", "link_blocks", {"from": blocker, "to": slug})
    for source in discovered_from or []:
        client.change(
            "mutations.gq", "link_discovered_from", {"from": slug, "to": source}
        )

    return {"slug": slug, "status": status, "repo": detected_repo}


@mcp.tool
def task_get(slug: str) -> dict | None:
    """Retrieve a single task by slug. Returns the full node or ``null``."""
    rows = client.read("read.gq", "get_task", {"slug": slug})
    return rows[0] if rows else None


@mcp.tool
def task_list(
    repo: str | None = None,
    status: TaskStatus | None = None,
    project_slug: str | None = None,
    parent: str | None = None,
    assignee: str | None = None,
) -> list[dict]:
    """
    List tasks, filtered by repo, status, project, parent, and/or assignee.

    ``project_slug`` and ``parent`` take precedence as the primary scope; other
    filters are applied on top in Python. With no filters, lists recent tasks
    across all repos.

    Parameters
    ----------
    repo:
        Canonical repo URI. Auto-detected if omitted.
    status:
        ``open`` | ``in_progress`` | ``blocked`` | ``closed``.
    project_slug:
        List the tasks of a WorkflowProject.
    parent:
        List the direct children of a parent task/epic.
    assignee:
        Filter to a single owner.
    """
    if project_slug:
        rows = client.read(
            "read.gq", "list_tasks_by_project", {"project_slug": project_slug}
        )
    elif parent:
        rows = client.read("read.gq", "list_tasks_by_parent", {"parent_slug": parent})
    else:
        detected = repo_module.detect(override=repo)
        if detected and status:
            rows = client.read(
                "read.gq",
                "list_tasks_by_repo_status",
                {"repo": detected, "status": status},
            )
        elif detected:
            rows = client.read("read.gq", "list_tasks_by_repo", {"repo": detected})
        elif status:
            rows = client.read("read.gq", "list_tasks_by_status", {"status": status})
        else:
            rows = client.read("read.gq", "list_all_tasks", {})

    if status:
        rows = [r for r in rows if r.get("status") == status]
    if assignee:
        rows = [r for r in rows if r.get("assignee") == assignee]
    return rows


@mcp.tool
def task_update(
    slug: str,
    title: str | None = None,
    description: str | None = None,
    type: TaskType | None = None,
    status: TaskStatus | None = None,
    priority: TaskPriority | None = None,
    assignee: str | None = None,
    project_slug: str | None = None,
    parent: str | None = None,
    external_uri: str | None = None,
    symbol_refs: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict | None:
    """
    Update a task's mutable fields. Only non-null arguments are applied.

    Use this to claim a task (``assignee``), move it to ``in_progress``, re-prioritise,
    re-parent (``parent``), or attach an ``external_uri``. To close a task prefer
    ``task_close``; to add dependencies use ``task_link``.
    """
    changes: dict = {}
    if title is not None:
        changes["title"] = title
    if description is not None:
        changes["description"] = description
    if type is not None:
        changes["type"] = type
    if priority is not None:
        changes["priority"] = priority
    if assignee is not None:
        changes["assignee"] = assignee
    if project_slug is not None:
        changes["project_slug"] = project_slug
    if external_uri is not None:
        changes["external_uri"] = external_uri
    if symbol_refs is not None:
        changes["symbol_refs"] = symbol_refs
    if tags is not None:
        changes["tags"] = tags
    if status is not None:
        changes["status"] = status
        if status == "closed":
            changes["closed_at"] = _now_iso()

    updated = _update_task(slug, changes)

    if parent is not None and updated is not None:
        client.change("mutations.gq", "link_parent_of", {"from": parent, "to": slug})
        updated = _update_task(slug, {"parent_slug": parent})

    return updated


@mcp.tool
def task_close(slug: str, resolution: str | None = None) -> dict | None:
    """
    Close a task: set status ``closed``, stamp ``closed_at``, record a resolution.

    Closing a blocker is what unblocks its dependents — they become visible to
    ``task_ready`` once every blocker is closed.
    """
    closed = _update_task(
        slug,
        {"status": "closed", "closed_at": _now_iso(), "resolution": resolution},
    )
    if closed:
        _unblock_dependents(closed.get("repo"))
    return closed


@mcp.tool
def task_ready(
    repo: str | None = None,
    project_slug: str | None = None,
    assignee: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Return ready-to-work tasks: not-yet-started tasks whose blockers are all closed.

    A task is ready when its status is ``open`` or ``blocked`` (i.e. nobody is on it
    yet and it is not closed) AND every task in its ``blocked_by`` list is closed.
    This is the core coordination primitive — call it to pick the next actionable
    item without manual triage. Results are ordered by priority (``p0`` first).

    Parameters
    ----------
    repo:
        Canonical repo URI. Auto-detected if omitted.
    project_slug:
        Restrict to a single WorkflowProject.
    assignee:
        Restrict to a single owner (or pass to find your own ready work).
    limit:
        Maximum tasks to return. Defaults to 20.
    """
    if project_slug:
        rows = client.read(
            "read.gq", "list_tasks_by_project", {"project_slug": project_slug}
        )
    else:
        detected = repo_module.detect(override=repo)
        rows = (
            client.read("read.gq", "list_tasks_by_repo", {"repo": detected})
            if detected
            else client.read("read.gq", "list_all_tasks", {})
        )

    status_by_slug = {r["slug"]: r.get("status") for r in rows}

    def blocker_status(blocker_slug: str) -> str:
        if blocker_slug in status_by_slug:
            return status_by_slug[blocker_slug] or "open"
        fetched = client.read("read.gq", "get_task", {"slug": blocker_slug})
        # A blocker that no longer exists does not hold anything back.
        return fetched[0].get("status", "closed") if fetched else "closed"

    ready = [
        r
        for r in rows
        if r.get("status") in ("open", "blocked")
        and all(blocker_status(b) == "closed" for b in (r.get("blocked_by") or []))
        and (assignee is None or r.get("assignee") == assignee)
    ]
    ready.sort(key=lambda r: _PRIORITY_ORDER.get(r.get("priority"), 9))
    return ready[:limit]


@mcp.tool
def task_link(from_slug: str, to_slug: str, kind: TaskLinkKind) -> dict:
    """
    Link two tasks (or a task to a memory).

    The meaning of ``from``/``to`` depends on ``kind``:
    - ``blocks``          — ``from`` is the blocker, ``to`` is the blocked task.
    - ``parent``          — ``from`` is the parent/epic, ``to`` is the child.
    - ``discovered_from`` — ``from`` is the new task, ``to`` is the source it came from.
    - ``addresses``       — ``from`` is the task, ``to`` is a Memory slug it addresses.

    For ``blocks`` and ``parent`` the denormalized ``blocked_by`` / ``parent_slug``
    fields on the affected task are kept in sync so ``task_ready`` stays correct.
    """
    if kind == "blocks":
        client.change("mutations.gq", "link_blocks", {"from": from_slug, "to": to_slug})
        blocked = client.read("read.gq", "get_task", {"slug": to_slug})
        if blocked:
            existing = blocked[0].get("blocked_by") or []
            if from_slug not in existing:
                changes = {"blocked_by": [*existing, from_slug]}
                if blocked[0].get("status") == "open":
                    changes["status"] = "blocked"
                _update_task(to_slug, changes)
    elif kind == "parent":
        client.change(
            "mutations.gq", "link_parent_of", {"from": from_slug, "to": to_slug}
        )
        _update_task(to_slug, {"parent_slug": from_slug})
    elif kind == "discovered_from":
        client.change(
            "mutations.gq", "link_discovered_from", {"from": from_slug, "to": to_slug}
        )
    elif kind == "addresses":
        client.change(
            "mutations.gq", "link_addresses", {"from": from_slug, "to": to_slug}
        )

    return {"from": from_slug, "to": to_slug, "kind": kind}


@mcp.tool
def context_for_symbol(symbol_id: str) -> dict:
    """
    Find the work-coordination context attached to a code-graph symbol.

    This is the reverse of the soft references stored by ``memory_store(symbol_refs=...)``
    and ``task_create(symbol_refs=...)``: given a Layer-2 symbol id, it returns the
    Layer-1 memories and tasks whose ``symbol_refs`` include it — e.g. "what lessons and
    open tasks concern this function?". Use it after locating a symbol with the
    omnigraph-codegraph ``code_*`` tools to pull the relevant knowledge before editing it.

    Parameters
    ----------
    symbol_id:
        A code-graph symbol id of the form ``repo#path/file.py::Qualified.Name``.
        The ``repo`` prefix (everything before ``#``) scopes the lookup; if the id
        carries no ``#`` the current repo is used.
    """
    repo = symbol_id.split("#", 1)[0] if "#" in symbol_id else repo_module.detect()

    if repo:
        mem_rows = client.read("read.gq", "memories_by_repo", {"repo": repo})
        task_rows = client.read("read.gq", "tasks_by_repo_refs", {"repo": repo})
    else:
        mem_rows = client.read("read.gq", "memories_with_refs", {})
        task_rows = client.read("read.gq", "tasks_with_refs", {})

    memories = [m for m in mem_rows if symbol_id in (m.get("symbol_refs") or [])]
    tasks = [t for t in task_rows if symbol_id in (t.get("symbol_refs") or [])]
    return {"symbol_id": symbol_id, "memories": memories, "tasks": tasks}
