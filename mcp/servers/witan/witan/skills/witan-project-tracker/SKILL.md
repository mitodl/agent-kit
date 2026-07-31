---
name: witan-project-tracker
description: >
  Track an end-to-end engineering project across multiple agent sessions
  without explicit handoffs. Use this skill when starting a multi-session
  project (call workflow_project_create then workflow_session_start), when
  continuing an existing project in a new session (call workflow_session_start
  with the slug injected by the context hook/extension), when advancing phases
  (workflow_project_advance), when closing a session (workflow_session_end),
  or when completing a project (workflow_project_complete to create a corpus
  trace). Requires the witan MCP server. Covers project lifecycle,
  cross-session linking, parallel sessions, phase transitions, and corpus trace
  creation for pattern mining.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Project Tracker

Tracks engineering work across sessions from discovery through delivery. Each
project is a `WorkflowProject` node in the witan graph. Each agent
session contributes a `WorkflowSession`. When a project completes, a
`WorkflowTrace` is assembled for the corpus.

The context-injection hook (Claude Code) / extension (Pi) auto-injects active
project context into every session in the repo, so you rarely need to find a
project slug manually.

## Starting a Session: Joining an Existing Project

When the hook injects context showing an active project that matches your work:

```
workflow_session_start(
    project_slug="wp-add-vault-k8s-auth-a3f912",
    session_id="<$CLAUDE_SESSION_ID on Claude Code, or any stable session UUID>",
    phase="implementation",
)
```

Link the session before doing substantive work. The phase should reflect what
this particular session will focus on, not necessarily the project's current
overall phase.

Hold on to the `session_slug` in the returned handle. Pass it to `memory_store`
and `workflow_trace_mine` so what you record this session is attributed to it, and
to `workflow_session_end` when you close. The protocol carries no session state,
so the handle is the only link between these calls.

## Starting a Session: Creating a New Project

If no active project in the injected context matches the work:

```
workflow_project_create(
    title="Add Vault K8s auth to ol-django",
    description="Wire Vault K8s auth using hvac for secret injection at runtime. Driven by compliance requirement to stop using static env secrets.",
    phase="discovery",
    github_issue="github.com/mitodl/ol-django/issues/847",
    tags=["vault", "kubernetes", "secrets"],
)
```

Then immediately start a session:

```
workflow_session_start(
    project_slug="<slug returned above>",
    session_id="<session UUID>",
    phase="discovery",
)
```

The call is **re-entrant**: if it returns `existed: true`, a session for this
`(project_slug, session_id)` was already open and you've been handed that same
handle back — safe to retry after a transport error without duplicating the
session. A newly-supplied `repo`/`tags` is merged in; `phase` stays at what the
first call set. Once a session has been ended, the same `session_id` starts a
fresh one, so several working stints under one Claude Code session each get
their own record.

### Multi-repo projects

A project can span several repos (e.g. a Django service, its frontend, and the
infra repo that deploys it) or none (a cross-cutting objective). Pass the set
via `repos`; the repo you create from is added automatically:

```
workflow_project_create(
    title="B2B self-serve analytics",
    description="StarRocks MVs + FastAPI service surfaced in the MIT Learn site",
    repos=[
        "https://github.com/mitodl/mit-learn",
        "https://github.com/mitodl/ol-infrastructure",
    ],
)
```

You don't have to list every repo upfront — the set **accretes**: whenever
`workflow_session_start` runs in a repo not yet in the set, that repo is added.
A project surfaces in the injected context of any repo in its set. Omit `repos`
entirely (from outside any git repo) to create a "floating" project tied to none.

Guessing at creation time is normal — a project's real blast radius is rarely
known during discovery — so the set is editable. Getting it right matters:
repo-scoped recall from a repo missing from the set will not surface the project
at all.

```
workflow_project_update(
    slug="wp-b2b-self-serve-analytics-8db781",
    add_repos=["https://github.com/mitodl/ol-analytics-api"],
    remove_repos=["https://github.com/mitodl/mit-learn"],   # guessed wrong
)
```

Pass `repos=[...]` instead to replace the set wholesale. Repos are a plain list
on the project node, so a removal really removes.

## Correcting a Project

`workflow_project_update` is the general escape hatch for metadata set wrong or
learned later. Every argument is optional and only what you pass is touched:

```
workflow_project_update(
    slug="wp-...",
    title="B2B self-serve analytics",       # renamed
    description="...",                      # scope grew
    tags=["analytics", "starrocks"],
    github_issue="https://github.com/mitodl/hq/issues/10594",
    status="abandoned",                     # work stopped without an outcome
)
```

Two things it deliberately can't do:

- **Set the phase** — use `workflow_project_advance`, which is also how a phase
  set in error is corrected (it allows going backwards).
- **Complete a project** — `status` takes `active` and `abandoned` only.
  `workflow_project_complete` stays the only route to a corpus trace, so a trace
  never exists without an outcome narrative.

## Phase Transitions

When the project moves between phases (e.g. spec is approved, implementation
begins):

```
workflow_project_advance(
    slug="wp-add-vault-k8s-auth-a3f912",
    phase="implementation",
)
```

Valid phases in order: `discovery` → `spec` → `implementation` → `delivery`.
You can skip phases for smaller tasks (e.g. discovery → implementation directly).
Phases do not need to be sequential; a project can return to a phase.

## Ending a Session

Before closing a session, record what was accomplished:

```
workflow_session_end(
    session_slug="ws-...",  # returned by workflow_session_start
    summary="Implemented hvac client wrapper and unit tests. Still need to wire env injection at app startup and add integration test. Decided to use lazy initialization pattern.",
    tools_used=["Edit", "Bash", "Read"],
    files_changed=["ol_django/vault.py", "tests/test_vault.py"],
)
```

The `Stop` hook auto-closes sessions that skip this call, but with a placeholder
summary. Calling `workflow_session_end` explicitly produces a richer corpus trace
and better context for the next session.

## Completing a Project

When the work is delivered:

```
workflow_project_complete(
    slug="wp-add-vault-k8s-auth-a3f912",
    outcome="Vault K8s auth fully wired. hvac reads secrets at pod startup via env injection. Static env secrets removed. Merged in PR #892. Integration tests passing in CI.",
    github_pr="github.com/mitodl/ol-django/pull/892",
)
```

This creates a `WorkflowTrace` — the immutable corpus record — and marks the
project `completed`. The trace node aggregates the project's `session_count`,
the distinct `phases` its sessions traversed, the total `duration` (hours), and
the `outcome` narrative (plus title/description/repos/tags). Per-session detail
like tools used and files changed lives on the individual `WorkflowSession`
nodes, not on the aggregate trace.

## Listing Projects

```
workflow_project_list()                       # active projects in this repo
workflow_project_list(status="completed")     # completed projects in this repo
workflow_project_list(repo="")                # active projects across all repos
```

## Parallel Sessions

Two agent sessions working on the same project simultaneously: each calls
`workflow_session_start` independently with the same `project_slug`. Sessions
are linked via separate `BelongsTo` edges — no coordination needed. Both sessions
will see the same project in their injected context.

When `workflow_project_complete` is called (by either session, after all sessions
have called `workflow_session_end`), all sessions are included in the trace. The
call is idempotent: the second call returns the existing trace without error.

## Corpus and Pattern Mining

Completed projects accumulate as `WorkflowTrace` nodes:

```
workflow_project_list(status="completed")   # list finished projects
```

Fetch a specific trace:

```
workflow_trace_get(slug="wp-add-vault-k8s-auth-a3f912")   # accepts the wp- or wt- slug
```

`workflow_trace_get` accepts either the project slug (`wp-…`) or the trace slug
(`wt-…`) — no need to hand-construct `wt-{project_slug}` — and returns `None`
until the project has been completed. `workflow_trace_list` is the discovery
path across many traces.

Traces record: phases traversed, session count, total duration, and the outcome
narrative (tools-used/files-changed are per-session, not on the trace). These
are the inputs for extracting reusable patterns and generating new skills.

## Linking Memories to Projects

When a session produces a pattern or lesson worth storing:

```
memory_store(kind="lesson", title="...", content="...")
# → returns {"slug": "les-..."}

# Then link it to the project so it shows up in the trace
workflow_project_link_memory(project_slug="wp-...", memory_slug="les-...")
```

## Slug Conventions

| Prefix | Type | Example |
|--------|------|---------|
| `wp-`  | WorkflowProject | `wp-add-vault-k8s-auth-a3f912` |
| `ws-`  | WorkflowSession | `ws-wp-add-vault-k8s-a3f912-7b2c41` |
| `wt-`  | WorkflowTrace   | `wt-wp-add-vault-k8s-auth-a3f912` |
