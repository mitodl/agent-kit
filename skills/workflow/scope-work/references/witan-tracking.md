# Tracking the work in witan

How [`scope-work`](../SKILL.md) establishes a piece of work in the
work-coordination graph. Tools are named alongside the skills that own them, because a consumer
who installed this catalog without witan has no `witan-task` SKILL.md for a
link to reach.

**There is no lookup by `external_uri`.** `task_search` is BM25 over title and
description only; `task_list` filters on repo, status, project, parent and
assignee. So work down this chain and create only when all four come up empty —
otherwise the skill forks a duplicate task on every re-run:

1. The injected session context, which already lists the in-flight branch,
   ready tasks, and active projects with their issue URLs.
2. `task_for_branch(branch="<current>")`.
3. `task_list(repo="<product repo>")`, filtered client-side on `external_uri`.
4. `task_search(query="<issue title>")`.

Under the tracker split this cuts across itself: the URL is `mitodl/hq#N` while
the task's `repo` is the product repo. **Match on the URL, scope on the product
repo.**

**Claiming.** `task_claim(slug=..., session_id=...)` — pass `session_id`, never
`assignee`, which replaces the holder outright and records a session with no
person behind it. Read the id from your own platform's variable only
(`echo $CLAUDE_SESSION_ID`, or `echo $PI_SESSION_ID`), not a fallback chain: an
agent launched from the other platform inherits its id. A refusal comes back as
`{"claimed": false, "held_by": ...}` — surface it and pick other work rather
than forcing past a live lease. If planning is abandoned before approval,
`task_release(slug=...)`.

**Claim timing.** `scope-work` makes no edits at all, and claims the task in
its Step 5, before handing off. That is deliberate: an unclaimed task being
actively worked is indistinguishable from an idle one, so the claim happens as
soon as the work is identified rather than when editing starts. If a spike is
needed to judge a mechanism, claim first and say so — a spike is an edit.

**Projects: join before creating.** `workflow_project_list()` first, join a
match with `workflow_session_start`, and create only when nothing matches. A
single-session fix needs a task, not a project. Use `workflow_project_advance`
for transitions; the phases map cleanly onto this skill:

| Skill | Phase |
|-------|-------|
| `scope-work` | `discovery` |
| `plan-technical-approach`, `write-adr` | `spec` |
| `implement-plan`, building chunks | `implementation` |
| `implement-plan`, close-out | `delivery` |

The session phase is what *this* session is doing and need not equal the
project's overall phase.

**Two GitHub link fields, kept consistent.** `Task.external_uri` holds the issue
URL; `WorkflowProject.github_issue` holds it separately, with `github_pr` set
later via `workflow_project_advance(github_pr=...)`.

Keep the `session_slug` that `workflow_session_start` returns. It is the only
thread linking this session's later calls, including `memory_store`.

## Phases, and what the later skills add

Project phases run `discovery` → `spec` → `implementation` → `delivery`,
skippable and revisitable. `scope-work` opens at `discovery`; the skills
downstream advance it with `workflow_project_advance` as they go. The session
phase is what *this* session is doing and need not equal the project's.

Two more things happen in the graph later, and are documented where they
happen, not here: [`plan-technical-approach`](../../plan-technical-approach/SKILL.md)
writes the epic and per-chunk child tasks when a plan is recorded, and
[`implement-plan`](../../implement-plan/SKILL.md) closes them as each chunk
merges and ends the session.
