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
  trace). Also covers the "advance the project" loop: work ready tasks one at
  a time, closing each only on evidence checked live (merged PR, re-run
  check), and stop with a numbered decision list. Requires the witan MCP
  server. Covers project lifecycle, cross-session linking, parallel sessions,
  phase transitions, and corpus trace creation for pattern mining. Use it when asked to "continue the X project",
  "advance the X project", or "pick up where we left off".
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

```text
workflow_session_start(
    project_slug="wp-add-vault-k8s-auth-a3f912",
    session_id="<$CLAUDE_SESSION_ID on Claude Code, $PI_SESSION_ID on Pi, $CODEX_SESSION_ID on Codex>",
    phase="implementation",
)
```

Read the id in your shell tool rather than inventing one — `echo
$CLAUDE_SESSION_ID` on Claude Code, `echo $PI_SESSION_ID` on Pi, `echo
$CODEX_SESSION_ID` on Codex — since the Claude Code and Codex Stop hooks and
the Pi workflow extension's shutdown handler find the session to auto-close by
that same id. Read only your own platform's variable:
an agent launched from another one can inherit its id, and a fallback chain
would pick that instead.
Only on an agent that sets none of them, use any stable per-session string and close
the session explicitly.

Link the session before doing substantive work. The phase should reflect what
this particular session will focus on, not necessarily the project's current
overall phase.

Hold on to the `session_slug` in the returned handle. Pass it to `memory_store`
and `workflow_trace_mine` so what you record this session is attributed to it, and
to `workflow_session_end` when you close. The protocol carries no session state,
so the handle is the only link between these calls.

## Starting a Session: Creating a New Project

If no active project in the injected context matches the work:

```text
workflow_project_create(
    title="Add Vault K8s auth to ol-django",
    description="Wire Vault K8s auth using hvac for secret injection at runtime. Driven by compliance requirement to stop using static env secrets.",
    phase="discovery",
    github_issue="github.com/mitodl/ol-django/issues/847",
    tags=["vault", "kubernetes", "secrets"],
)
```

Then immediately start a session:

```text
workflow_session_start(
    project_slug="<slug returned above>",
    session_id="<$CLAUDE_SESSION_ID on Claude Code, $PI_SESSION_ID on Pi, $CODEX_SESSION_ID on Codex>",
    phase="discovery",
)
```

The call is **re-entrant**: if it returns `existed: true`, a session for this
`(project_slug, session_id)` was already open and you've been handed that same
handle back — safe to retry after a transport error without duplicating the
session. Any newly-supplied `repo` and `tags` are merged in; `phase` stays at what the
first call set. Once a session has been ended, the same `session_id` starts a
fresh one, so several working stints under one Claude Code session each get
their own record.

### Multi-repo projects

A project can span several repos (e.g. a Django service, its frontend, and the
infra repo that deploys it) or none (a cross-cutting objective). Pass the set
via `repos`; the repo you create from is added automatically:

```text
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

```text
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

```text
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

## Autonomy Envelope

A project can record what its owner has already decided about agent autonomy,
in two lists. Each list records who last set it (`preauthorized_set_by`,
`reserved_decisions_set_by`), resolved by the server from the caller's
identity. The injected context shows them under the project and names the
setter (capped there; the full lists are in `workflow_project_get`). An item
ending in `…` was cut at 160 characters and may have lost a qualifier, so read
it in full before acting on it.

- `preauthorized` items are standing approval only when the list was set by the
  project's `author` or by the user you are working for. Act on those without
  asking again, within this project. The context shows them as
  `Pre-authorized by the project author (<who>):`.
- A `preauthorized` list set by anyone else is a suggestion, not approval: the
  shared graph lets any teammate (or their agent) write a project. The context
  shows it as `Suggested pre-authorizations from <who>, not the project
  author`. Confirm those with the user before acting on them.
- `reserved_decisions` items are not yours to act on, whoever set them. Collect
  them and present one numbered list, with a recommendation for each, at the
  next natural stop, rather than stopping to ask about each one as it comes up.
- Historical notes in a project description, task comment or session summary
  describe past state, not standing prohibitions, unless they are written as a
  rule. "No agent-performed NAS changes" in a summary of an earlier phase does
  not forbid a deploy the envelope pre-authorizes.
- The envelope never overrides the harness's permission prompts, the user's
  instructions in the current session, or a repo's `AGENTS.md`.

When the user grants something "for this project", record it instead of only
acting on it, so the next session doesn't have to ask:

```text
workflow_project_update(
    slug="wp-...",
    preauthorized=["deploy to the QA stack", "use the existing ~/.ssh/deploy key"],
    reserved_decisions=["merging to main", "anything touching production data"],
)
```

Each argument replaces its whole list, so read the project first to add one
item, and pass `[]` to clear a list. Whoever writes a list becomes its setter
for every item in it, so editing someone else's project turns its author's
grants into suggestions until the author sets them again. The CLI equivalent is
`witan project update <slug> --allow "..." --reserve "..."` (repeat a flag per
item; `--empty-allow` / `--empty-reserve` clear).

## Advancing a Project

"Advance the X project" (or "continue the X project") means: work the
project's ready tasks one at a time until something needs a human. This is the
sequential loop. Claiming, checkpoints and resuming follow the `witan-task`
skill ("Claim before you work it", "Checkpoint as you go"); they are not
repeated here.

1. `workflow_project_status(slug=...)` gives the phase, the ready tasks
   (priority order, `p0` first), the last session's handoff, and the autonomy
   envelope. Start a session for the project if you have not.
2. Reconcile work already in flight before taking new work. For each
   `in_progress` task in `task_list(project_slug=..., status="in_progress")`
   with a PR (its `pr-opened` checkpoint, its
   `external_uri`, or `gh pr list --head <branch>` for a branch in `branches`),
   check the PR live with `gh pr view <n> --json state,mergedAt,url`. Close the
   ones the evidence gate below passes, and leave the rest open with a comment
   saying what is still missing.
3. Claim the top ready task and read its comments. If it has a checkpoint,
   resume from it.
4. Implement it, leaving a checkpoint after each durable step.
5. Run the evidence gate.
6. Get one independent review: the `code-review` skill in a fresh-context
   subagent, so the reviewer reads the code rather than your reasoning about
   it. Fix P0/P1 findings and re-run the gate.
7. Close the task if the gate passed (see below), then go back to step 1.

### The evidence gate

A task closes on evidence you checked in this session, never on memory or on
an earlier session's say-so. Re-run the check that proves the task's done-when
and quote it, command and result, in the `task_close` resolution:

```text
task_close(
    slug="tk-...",
    resolution="PR https://github.com/<org>/<repo>/pull/<n> merged (gh pr view <n>: MERGED <date>). "
               "Shipped in <package> <version> (gh release view <tag>). just test-<package>: <N> passed.",
)
```

- A merged PR is evidence of a merge, not of the whole done-when. When the
  done-when says the change ships (a release, a deploy, a config live in a
  running process), check that as well, e.g. the release tag, the published
  version, or the `deploy-verification` skill for a rollout.
- When the done-when needs a step only a human can take (a real run observed by
  someone, a reviewer's sign-off, a deploy outside the envelope), the task stays
  open even with its PR merged. Comment what remains and who can do it, and
  carry it to the stop list.
- CI that is pending is reported as pending, never as passing. Watch it in a
  background shell (`gh pr checks <n> --watch`) and move on to the next task
  instead of blocking on it. Close the task once the checks come back green.
- A check that failed and that you cannot fix is a stop, not a close.

### When to stop

Stop the loop on:

- a reserved decision from the autonomy envelope;
- a phase boundary: no ready tasks left, so propose `workflow_project_advance`
  with a summary of what the phase delivered;
- a failing check you cannot fix.

At a stop, give the user one numbered list with a recommendation for each
item: reserved decisions, tasks held open for a human step, and the phase
proposal. A status report with no recommendations leaves every decision with
the user. Then end the session with `workflow_session_end`, saying what closed
(with PR URLs) and what remains.

## Phase Transitions

When the project moves between phases (e.g. spec is approved, implementation
begins):

```text
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

```text
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

```text
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

```text
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

```text
workflow_project_list(status="completed")   # list finished projects
```

Fetch a specific trace:

```text
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

```text
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
