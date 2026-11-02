# Tracking the work in witan

How [`scope-work`](../SKILL.md) establishes a piece of work in the
work-coordination graph.

**This file says *when* to claim, advance and end. It does not say *how*.**
Those rules belong to the witan skills, which install alongside this one — so
follow the link rather than a copy:

| For | Read |
|-----|------|
| Claiming, releasing, comments, checkpoints | [`witan-task`](../../witan-task/SKILL.md) |
| Linking a session to a project, and asking before touching the graph | [`witan-workflow`](../../witan-workflow/SKILL.md) |
| Project lifecycle, phases, multi-repo projects | [`witan-project-tracker`](../../witan-project-tracker/SKILL.md) |

This used to restate them, on the grounds that a consumer who installed the
catalog without witan would have no `witan-task` SKILL.md for a link to reach.
That reasoning does not hold: all four of these skills stop outright when the
witan tools are missing, and `witan setup` installs the witan skills *with* the
tools — these skills now ship in that same bundle. The copies had already
drifted apart by the time anyone checked, which is the actual cost: `witan-task`
requires reading a task's comments when you claim it, because a comment outranks
the description, and the copy here had never been updated to say so.

## Finding the task for an issue

**There is no lookup by `external_uri`.** `task_search` is BM25 over title and
description only; `task_list` filters on repo, status, project, parent and
assignee. So work down this chain and create only when all four come up empty —
otherwise the skill forks a duplicate task on every re-run:

1. The injected session context, which already lists the in-flight branch,
   ready tasks, and active projects with their issue URLs.
2. `task_for_branch(branch="<current>")`.
3. `task_list(repo="<product repo>")`, filtered client-side on `external_uri`.
4. `task_search(query="<issue title>")`.

Step 3 pulls every task in the repo into context to match one URL. That is
expensive and it is the lookup everything else needs too, so it is being
replaced by an `external_uri` filter on `task_list`; until that lands, the
chain stands.

Under the tracker split this cuts across itself: the URL is `mitodl/hq#N` while
the task's `repo` is the product repo. **Match on the URL, scope on the product
repo.**

## When to claim

`scope-work` makes no edits at all, and claims the task in its Step 5, before
handing off. That is deliberate: an unclaimed task being actively worked is
indistinguishable from an idle one, so the claim happens as soon as the work is
identified rather than when editing starts. If a spike is needed to judge a
mechanism, claim first and say so — a spike is an edit.

How to claim, what a refusal looks like, which id to pass, and the requirement
to **read the task's comments first** are all in
[`witan-task`](../../witan-task/SKILL.md). If planning is abandoned before
approval, release it.

## When to join or create a project

A single-session fix needs a task, not a project. Where the work *is* a
project, join an existing one before creating a new one —
`workflow_project_list()` first.

**Ask before either.** `witan-workflow` deliberately puts nothing in the graph
until the user has answered, and this skill is not an exception to that; this
repo alone has 27 active projects, which is what skipping the question looks
like after a while.

Make it one question, not an interview. By the time Step 5 runs, this skill
already knows the issue, the product repo and the tier, so propose the project
— the existing one to join, or a new one with every field filled in — and ask
for a single confirmation. Ask it the way
[`witan-workflow`](../../witan-workflow/SKILL.md) specifies, so the behaviour is
the same on every agent.

## Phases

Project phases run `discovery` → `spec` → `implementation` → `delivery`,
skippable and revisitable. `scope-work` opens at `discovery`; the skills
downstream advance it with `workflow_project_advance` as they go:

| Skill | Phase |
|-------|-------|
| `scope-work` | `discovery` |
| `plan-technical-approach`, `write-spec`, `write-adr` | `spec` |
| `implement-plan`, building chunks | `implementation` |
| `implement-plan`, close-out | `delivery` |

`spec` has a concrete meaning on the spec route: **the spec document exists and
is under review.** On the other routes it is the interval between an approach
being approved and the first chunk starting.

The session phase is what *this* session is doing and need not equal the
project's overall phase.

## Two GitHub link fields, kept consistent

`Task.external_uri` holds the issue URL; `WorkflowProject.github_issue` holds it
separately, with `github_pr` set later via
`workflow_project_advance(github_pr=...)`.

## What the later skills add

Documented where they happen, not here:
[`plan-technical-approach`](../../plan-technical-approach/SKILL.md) promotes
this task to an epic and adds a child task per chunk when a plan is recorded,
and [`implement-plan`](../../implement-plan/SKILL.md) closes them as each chunk
merges and ends the session.
