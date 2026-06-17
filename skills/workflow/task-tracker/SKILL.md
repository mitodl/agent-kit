---
name: task
description: >
  Interactive task manager for the work-coordination graph. Use to triage and
  claim ready work, create tasks (including epics and sub-issues), link
  dependencies, and close finished tasks. `/task` shows ready work for the
  current repo; `/task new` creates a task; `/task list` lists tasks; `/task
  close` closes one. Backed by the omnigraph-memory MCP server (task_* tools).
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
MCP call fails, tell the user the omnigraph-memory server is not connected and stop.

- If there are no ready tasks, say so and offer **Create a task**.
- Otherwise present the ready tasks in an `AskUserQuestion`:
  - Header: "Claim task"
  - Question: "Which task do you want to work on?"
  - Options: each ready task (label = title, description = "`[priority]` slug: `{slug}`"),
    plus "Create a task" and "None".
- On a chosen task: ask whether to claim it. To claim, call
  `task_update(slug="<slug>", status="in_progress", assignee="<author>")` where
  `<author>` is `$OMNIGRAPH_MEMORY_AUTHOR` (or the git user name). Confirm:
  "Claimed **{title}** (`{slug}`). Close it with `/task close` when done."

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
