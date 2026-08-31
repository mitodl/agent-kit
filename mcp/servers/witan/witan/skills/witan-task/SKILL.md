---
name: witan-task
description: >
  Interactive task manager for the work-coordination graph. Use to triage and
  claim ready work, create tasks (including epics and sub-issues), link
  dependencies, and close finished tasks. Also use it whenever you are about to
  START work on an existing `tk-` task from anywhere — the ready-task list in
  your session context, a `witan project run` prompt, or a slug someone named —
  because that task must be claimed before the first edit. Also use it to leave
  feedback on a task you are NOT executing — a wrong premise, a mechanism that
  cannot fire, work already done elsewhere — via `task_comment`, rather than
  editing someone else's description or filing a task that is not work.
  `/witan-task` shows ready work for the current repo; `/witan-task new`
  creates a task; `/witan-task list` lists tasks; `/witan-task close` closes
  one. Backed by the witan MCP server (task_* tools).
license: BSD-3-Clause
metadata:
  category: workflow
---

# Task Manager

Interactive entry point for the dependency-aware task tracker. Tasks live in the
same graph as workflow projects and memory, so they can roll up to a project
(`project_slug`), nest under an epic (`parent`), block one another (`blocked_by`),
and reference code-graph symbols (`symbol_refs`).

The repo key is auto-detected from `.git/config` (the canonical HTTPS URI) — you
rarely pass `repo` explicitly.

## Claim before you work it

**Any task you actually start gets claimed first — `task_claim(slug="tk-...")`
before the first edit, every time.** This is not limited to tasks you reached
through this skill. It applies just as much to one you picked off the ready
list in your session's injected context, one named in a `witan project run`
prompt, or one a human mentioned by slug.

The claim is the only signal that anybody is on it. `in_progress` with a live
lease is what makes `task_ready` hide it from the next session and what makes
`task_claim` refuse a second holder — an unclaimed task being actively worked
looks exactly like one nobody has touched. Two sessions have already written
the same fix for the same task on the same day, each unaware of the other,
because neither claimed it first.

So:

- **Claim it** when you decide to work it, not when you finish. A claim after
  the fact records history; it prevents nothing.
- **Take the refusal seriously.** `{"claimed": false, "held_by": ...}` means
  someone is on it. Pick another task — do not `force` past a live lease
  without a reason you can state.
- **Pass `session_id`, not `assignee`.** `task_claim(slug=..., session_id=...)`
  — your `$CLAUDE_SESSION_ID` on Claude Code, any stable per-run id elsewhere.
  It qualifies the holder as `<you>#<session>` so your own parallel sessions
  are told apart; without it they all claim under one name, the contention
  check cannot separate them, and the second session silently renews the
  first's lease and is told it claimed. A deployed witan cannot infer the id
  (a pod has no `$CLAUDE_SESSION_ID`, and MCP carries no session state), so an
  agent talking to it directly has to send it — the CLI's proxy does this for
  you, an MCP client does not. A success with `"qualified": false` and a
  `"warning"` is the server telling you this happened.
  Do **not** put the session id in `assignee`: that replaces the holder
  outright, so the claim records a session and no person. Reserve `assignee`
  for a genuinely different worker identity (a CI job, a named runner).
- **Release what you drop.** `task_release(slug=...)` if you claim something
  and then move on, so it returns to ready work instead of ageing out.
- **Claim as you go, not in bulk.** When handed a list, claim each task as you
  reach it. Claiming all of them up front holds leases on work you may never
  start, which is the same lie in the other direction.

`witan task run` already claims before it launches the agent, so a session
started that way arrives holding its task; its prompt says so.

**Read the comments when you claim.** `task_get(slug=...)` returns a `comments`
list alongside the description. A comment is how another agent tells you the
task's own text is wrong, so treat it as outranking the description it sits
next to — a task that says "add a check that fails during `pulumi preview`" and
carries a comment saying that preview never runs on those pipelines is not a
task to execute as written.

## Comment on someone else's task

When you find a problem with a task you are not executing, say it on the task:

```
task_comment(slug="tk-...", text="<the correction, with evidence>")
```

The comment is attributed to you, timestamped, and append-only — it cannot be
edited or deleted, and it does not touch the task. It shows up in `task_get`,
and in the holder's injected session context if they are actively working it.

Reach for it instead of the two things that used to be the only options:

- **Not `task_update`.** Rewriting the description destroys another author's
  text and leaves no trace of who disagreed or why.
- **Not `task_create`.** A task whose real content is "your premise is wrong"
  is not work. It lands in everyone's ready-work list, has to be filed at the
  parent's priority to sit near it, and closing it means "I read this" — which
  is not what closing a task means anywhere else.

A comment is read once, by whoever executes that task. If what you found is
reusable knowledge about the repo, that is a `memory_store` (a project fact or
lesson) — and it is fine to do both: store the fact, then comment with the
correction and a pointer to it.

## When to use this vs. your built-in todo list

Use `task_*` for **work that outlives the current session or is shared** — items
others (or a future session) should see, things with dependencies/blockers, or an
epic's sub-issues. For the step-by-step checklist of the task you're doing *right
now*, use your built-in todo list; don't mirror those ephemeral steps into the
graph.

## On invocation

**Step 1 — Check args.**

- `list` → go to **List tasks**.
- `new` → go to **Create a task**.
- `close` → go to **Close a task**.
- otherwise (no args) → go to **Triage ready work**.

## Triage ready work

Call `task_ready()` (defaults to the current repo, ordered by priority). If the
MCP call fails, tell the user the witan server is not connected and stop.

- If there are no ready tasks, say so and offer **Create a task**.
- Otherwise present the ready tasks in an `AskUserQuestion`:
  - Header: "Claim task"
  - Question: "Which task do you want to work on?"
  - Options: each ready task (label = title, description = "`[priority]` slug: `{slug}`"),
    plus "Create a task" and "None".
- On a chosen task: claim it (that is what picking it means — see **Claim
  before you work it**). Call
  `task_claim(slug="<slug>", session_id="<$CLAUDE_SESSION_ID>")`.
  `task_claim` sets `in_progress` with a lease and **refuses if someone else
  holds it** (`{"claimed": false, "held_by": ...}`) — surface that and offer
  another task instead of overwriting. On success confirm: "Claimed **{title}**
  (`{slug}`). Close it with `/witan-task close`, or `task_release` it if you step away."
  Note: claims are advisory (a true atomic lock is a tracked follow-up), so a
  dead worker's claim auto-frees after its lease lapses and reappears in ready work.

## Create a task

Ask in one `AskUserQuestion` call (use "Other" for free text):

1. "Task title?"
2. "Type?" — options: `task`, `bug`, `feature`, `chore`, `epic`.
3. "Priority?" — options: `p2` (default), `p0`, `p1`, `p3`.

Then gather, if relevant: a one-line description, a parent epic slug
(`parent`), blocker slugs (`blocked_by`), a GitHub issue/PR URL
(`external_uri`), and a project slug (`project_slug`). Call:

```
task_create(
    title="<title>",
    description="<description>",
    type="<type>",
    priority="<priority>",
    parent="<epic slug or omit>",
    blocked_by=["<slug>", ...],         # omit if none
    external_uri="<github issue/PR or omit>",
    project_slug="<wp- slug or omit>",
)
```

For an **epic with sub-issues**: create the epic first (`type="epic"`), then
create each child with `parent="<epic slug>"`. Report the new slug(s).

## List tasks

Call `task_list()` (optionally `task_list(status="open")`,
`task_list(project_slug="<wp->")`, or `task_list(parent="<epic>")` to see an
epic's children). Print a table: slug, title, type, status, priority,
assignee, blocked_by, external_uri. Group sub-issues under their parent when a
`parent` filter is used.

## Close a task

Ask which task (slug) and an optional resolution note, then:

```
task_close(slug="<slug>", resolution="<what was done / outcome>")
```

Closing a blocker automatically makes its dependents eligible for `task_ready`.
Confirm and, if useful, run `task_ready()` again to show what just unblocked.

## Linking after the fact

To add a dependency, hierarchy, or provenance link to existing tasks, use
`task_link(from_slug, to_slug, kind)`:

- `blocks` — `from` blocks `to`
- `parent` — `from` (epic) is the parent of `to`
- `discovered_from` — `to` is the source `from` was discovered from
- `addresses` — `from` (task) addresses `to` (a Memory slug)

## Undoing a link

`task_unlink(from_slug, to_slug, kind)` takes the same arguments and removes
the link. Reach for it when one was recorded the wrong way round — the usual
tell is a task showing as blocked by something it actually blocks.

Removing the last `blocks` link returns the task from `blocked` to `open`, so
it shows up in `task_ready()` again. If the link wasn't there, the call
reports `removed: False` and changes nothing; re-running is safe.

## Linking tasks to code symbols

For a task scoped to specific code, attach code-graph **symbol ids** via
`symbol_refs` so the work is discoverable from the code. Get ids from the
`witan-code` tools — `code_find_definition` / `code_search_symbol`
return them in the `symbol_id` field (`<repo>#<path/to/file.py>::<QualifiedName>`):

```
task_create(
    title="Refactor Service.run for lazy init",
    description="...",
    symbol_refs=["https://github.com/mitodl/ol-django#app/svc.py::Service.run"],
)
```

`symbol_context(symbol_id)` lists the tasks and memories attached to a
symbol — call it before editing that code to surface related open work.
