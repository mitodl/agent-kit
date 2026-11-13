"""The server instructions every witan MCP surface sends at ``initialize``.

Its own module so ``witan.remote.serve`` can republish it without importing
``witan.server``, whose import opens a local graph. Before this, an agent
behind ``witan serve`` on a deployed target got no instructions at all, and
the tool descriptions that point here ("see instructions") pointed at nothing.

Text shared by several tools lives here once instead of in each parameter's
description: a client that defers tool schemas loads a tool's full schema on
every search hit, so repeated prose costs on every load, while the
instructions arrive once per session.
"""

from __future__ import annotations

__all__ = ["INSTRUCTIONS", "with_code_instructions"]

INSTRUCTIONS = (
    "Team-wide, shared, persistent memory and work-coordination graph. PREFER "
    "storing durable, shareable knowledge here — project facts, patterns, "
    "lessons, decisions, and hand-off context — over your private "
    "built-in/session memory, so other agents, future sessions, and teammates "
    "can find it. Record what you learn with memory_store. Also tracks "
    "workflow projects, sessions, and tasks.\n\n"
    "Loading context: call recall(query=…) — it composes every memory read "
    "(BM25 + graph expansion + superseded-pruning + re-ranking) and degrades "
    "to a plain search when the graph has no edges. Seed it with symbol_id, "
    "task, topic, contract, or memory instead of/along with query; those "
    "seeds also return a section about the seed itself:\n"
    "  symbol_id=… — also the tasks attached to a code symbol\n"
    "  topic=…     — also the Topic node (everything tagged to it, cross-repo)\n"
    "  contract=…, contract_kind=… — also the code providing/consuming the key\n"
    "  memory=…    — also its neighbours by edge kind and the code symbols it "
    "names\n"
    "Pass hops=0, include_superseded=True and a high limit to list exactly "
    "what a seed resolves to. Reach for a narrower read only for a specific "
    "need:\n"
    '  recall           — default; contextual load / "what do we know about X"\n'
    "  memory_get       — one memory by slug\n"
    "  memory_list      — browse all of one kind (no query); optional language\n"
    "  memory_search    — plain BM25, no graph expansion\n"
    "  memory_contradictions — every contradicting pair, no seed needed\n"
    "  workflow_project_memories — memories a project produced\n\n"
    "Repo scoping: every tool auto-detects the current repo from .git/config. "
    "Pass repo (a canonical URI like https://github.com/mitodl/ol-django) to "
    "override it. To span every repo, omit repo and pass all_repos=true; on "
    "memory_store, task_create and workflow_session_start that files the row "
    "under no repo (repo-scoped task reads include such tasks, repo-scoped "
    "memory reads do not include such memories). Never pass an empty repo: "
    "it is refused. When memory_list detects no repo and none is passed, it "
    "returns slim records (slug, kind, title, tags — no content); memory_get a "
    "slug for the full memory.\n\n"
    "Shared parameters:\n"
    "  session_id: your agent session id: $CLAUDE_SESSION_ID, or under Pi "
    "and Codex the value of `echo $PI_SESSION_ID` / `echo $CODEX_SESSION_ID` "
    "(they do not pass it to MCP servers); on any other client, a stable "
    "unique string for the session. witan's CLI proxy fills it in and a "
    "local stdio server falls back to its own environment; pass it yourself "
    "when calling a deployed witan. On reads it is only logged.\n"
    "  session_slug: the ws- handle workflow_session_start returned, not the "
    "agent session id. Records which session produced a write; a local stdio "
    "server finds it itself.\n"
    "  assignee: a holder identity. Defaults to you, qualified by "
    'session_id so your parallel sessions stay distinct. As a filter, "@me" '
    "is every session of the calling identity.\n"
    "  symbol_id / symbol_refs: witan-code symbol ids, "
    "repo#path/to/file.py::Name.\n"
    "  Slug prefixes: tk- task, wp- project, ws- session, wt- trace, tp- "
    "topic; pat-/pf-/les-/ctx- memory (pattern, project_fact, lesson, "
    "agent_context).\n\n"
    "Shared result fields:\n"
    "  ui_url: against a deployment that serves the web UI, the link "
    "that opens a task or project there. Hand a person that link rather than "
    "building one.\n"
    "  live_pr: when witan runs on your machine and a task's "
    "external_uri or a project's github_pr is a GitHub PR or issue, its state "
    'fetched just now, or {"error": …}. Where it disagrees with the '
    "description or comments, live_pr is current.\n"
    "  lease_expired: on an in_progress task: its claim lapsed, so task_ready "
    "offers it for reclaim.\n\n"
    "Task writes are compare-and-swap, fenced on the graph head and retried. "
    "Running out of attempts raises WriteContention with nothing written; "
    "retrying is safe. A call whose outcome is unknown (a timeout, a lost "
    "response) may still have landed, so re-read with task_get before "
    "retrying it.\n\n"
    "Changing memory — pick by what is actually wrong:\n"
    "  a field is wrong (wrong repo, typo'd title) → memory_update(slug, …); "
    "only the fields you pass change\n"
    "  the knowledge changed → memory_store the new one, then "
    'memory_link(from_slug=<new>, to_slug=<old>, kind="supersedes") — the old '
    "one is hidden from default reads but kept (include_superseded=True to "
    "see it)\n"
    "  it should never have existed (accidental duplicate, test write) → "
    "memory_delete(slug, confirm=True); author-only, hard delete\n"
    "  it contains a secret → rotate the credential. memory_delete removes it "
    "from the current graph but not from history, and no tool can erase "
    "that.\n\n"
    "Found a problem with someone else's task? task_comment(slug, body) "
    "leaves an attributed, append-only note ON it. Do NOT reach for "
    "task_update (it overwrites their description) or task_create (it puts "
    "an item that is not work into everyone's ready-work list). Comments "
    "come back in task_get, so read them before executing a task you "
    "claimed — they are often a correction to its own description.\n\n"
    "Naming: the task_* tools track work items (task_create, task_claim, "
    "task_ready, …) and have nothing to do with MCP's own tasks/* extension "
    "for long-running calls. A task_* slug is a unit of work someone is "
    "assigned; an MCP task id is a handle on a call still executing.\n\n"
    "Errors: a lookup that finds nothing returns null/empty, never raises; an "
    "invalid-but-well-formed mutation (self-link, claim "
    "contention) returns a status object with a reason; only malformed input "
    "raises."
)


def with_code_instructions(instructions: str | None, code_instructions: str) -> str:
    """What ``witan serve`` sends once witan-code is mounted: witan's, then its."""
    return f"{instructions}\n\n{code_instructions}"
