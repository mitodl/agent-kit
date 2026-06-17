---
name: project-tracker
description: >
  Track an end-to-end engineering project across multiple agent sessions
  without explicit handoffs. Use this skill when starting a multi-session
  project (call workflow_project_create then workflow_session_start), when
  continuing an existing project in a new session (call workflow_session_start
  with the slug injected by the context hook/extension), when advancing phases
  (workflow_project_advance), when closing a session (workflow_session_end),
  or when completing a project (workflow_project_complete to create a corpus
  trace). Requires the omnigraph-memory MCP server. Covers project lifecycle,
  cross-session linking, parallel sessions, phase transitions, and corpus trace
  creation for pattern mining.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Project Tracker

Tracks engineering work across sessions from discovery through delivery. Each
project is a `WorkflowProject` node in the omnigraph-memory graph. Each agent
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
project `completed`. The trace includes all sessions, their phases, tools used,
files changed, and the outcome narrative.

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
# Use workflow_project_get to get the project, then construct trace slug as wt-{project_slug}
```

Traces record: phases traversed, session count, total duration, tools used per
session, files changed, and the outcome narrative. These are the inputs for
extracting reusable patterns and generating new skills.

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
