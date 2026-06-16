---
name: workflow
description: >
  Interactive workflow session manager. Call at the start of any session to
  link it to a tracked project. Lists active WorkflowProjects for the current
  repo, lets the user pick one (or create a new project), then calls
  workflow_session_start to register the session in the graph. Also supports
  ending a session (/workflow end) or listing all projects (/workflow list).
  Requires the omnigraph-memory MCP server.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Workflow Session Manager

Interactive entry point for the workflow tracking system. Invoke with `/workflow`
at the start of a session, or `/workflow end` before stopping.

## On invocation

**Step 1 — Check args.**

If the user passed `list`: call `workflow_project_list()` with no filters,
print a table of all projects (slug, title, phase, status, repo), and stop.

If the user passed `end`: go to the **End session** section below.

If the user passed `new`: skip to **Create new project** below.

Otherwise (no args or unrecognized): proceed to Step 2.

**Step 2 — List active projects.**

Call `workflow_project_list()` (no args — defaults to active projects in the
current repo). If the MCP call fails, tell the user the omnigraph-memory server
is not connected and stop.

**Step 3 — Ask the user what to link to.**

Build an `AskUserQuestion` with one question:

- Header: "Link session"
- Question: "Which project does this session belong to?"
- If there are 1–3 active projects: offer each as a labelled option
  (label = title, description = "slug: `{slug}` · phase: {phase}"), plus
  "Create new project" and (if >0 projects exist) "None / untracked".
- If there are 0 active projects: offer "Create new project" and
  "None / untracked" only.
- If there are 4+ active projects: list them as text, ask the user to type a
  slug or "new", then use `workflow_project_get(slug)` to confirm the choice.

**Step 4 — Handle the response.**

If **"None / untracked"**: do nothing, stop. The session won't be tracked.

If **"Create new project"**: go to **Create new project** below, then
continue to Step 5.

Otherwise: the user picked an existing project. Note its slug and phase.

**Step 5 — Ask for the session phase.**

Ask: "What phase is this session?" with options matching the project's valid
phases: `discovery`, `spec`, `implementation`, `delivery`. Pre-select the
project's current phase as the default (first option, labelled "(current)").

**Step 6 — Start the session.**

Call:

```
workflow_session_start(
    project_slug="<chosen slug>",
    session_id="<value of CLAUDE_SESSION_ID env var>",
    phase="<chosen phase>",
)
```

To get `CLAUDE_SESSION_ID`: run `echo $CLAUDE_SESSION_ID` via Bash. If the
variable is empty, use a short random hex string instead (read `/proc/sys/kernel/random/uuid`
and take the first 8 chars).

Confirm to the user: "Session linked to **{title}** (`{session_slug}`). Call
`/workflow end` before you stop, or the Stop hook will auto-close it with a
placeholder summary."

---

## Create new project

Ask the user two questions in one `AskUserQuestion` call:

1. "Project title?" (free text — use "Other" as the entry point)
2. "Starting phase?" (options: discovery, spec, implementation, delivery)

Then call:

```
workflow_project_create(
    title="<title>",
    description="<brief description — ask if not obvious from the title>",
    phase="<phase>",
)
```

Return the new project slug and continue to Step 5 of the main flow.

---

## End session

Call `workflow_project_list()` to find active projects in this repo, then
check `/tmp/workflow-session-$CLAUDE_SESSION_ID.json` for the current session
state file. If found, read the `session_slug`.

Ask the user for a session summary: "Briefly describe what this session
accomplished (will be saved to the workflow corpus)."

Call:

```
workflow_session_end(
    session_slug="<slug from state file>",
    summary="<user's summary>",
    files_changed=["<git diff --name-only HEAD, limited to 50 files>"],
)
```

Confirm: "Session closed. The Stop hook will not need to auto-close it."
