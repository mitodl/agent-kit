# Pi Extensions

Pi has no Claude-style hooks, but its [extension events](https://pi.dev/docs/latest/extensions#events)
provide the equivalent triggers. These extensions mirror the Claude Code hooks
so the omnigraph trackers work the same under Pi.

| Extension | Pi events | Mirrors Claude hook(s) |
|---|---|---|
| `extensions/codegraph.ts` | `session_start`, `tool_call`/`tool_result` (`edit`/`write`), `before_agent_start`, `session_shutdown` | codegraph-session-init + codegraph-reindex + codegraph-context + codegraph-checkpoint |
| `extensions/workflow-context.ts` | `before_agent_start` | workflow-context-inject |

- **codegraph.ts** (owned by witan-code; see
  [mcp/servers/witan-code/witan_code/extensions/pi/codegraph.ts](../../mcp/servers/witan-code/witan_code/extensions/pi/codegraph.ts),
  installed by `witan-code setup --agent pi`) —
  - `session_start`: seeds/refreshes the whole repo's Layer-2 code graph in the background.
  - `tool_call`/`tool_result` (`edit`/`write`): incrementally re-indexes the edited file.
  - `before_agent_start`: reports whether the code graph is indexed (file count,
    last-updated) or still being built, with a nudge toward `code_*` tools over grep.
  - `session_shutdown`: opportunistically compacts the current repo's store and
    the shared bridge store (throttled) — unlike the workflow checkpoint below,
    this has no session-id dependency, so it *is* mirrored under Pi.

  Requires `witan-code` on `PATH` (`witan-code setup --agent pi`, or
  `uv tool install --editable mcp/servers/witan-code`); otherwise no-ops.
- **workflow-context.ts** — before each turn, appends the repo's active
  WorkflowProjects and ready tasks to the system prompt. Requires the `omnigraph`
  binary and the witan graph.

Both are best-effort: any failure (missing binary, non-git dir, no data) is
swallowed and never disrupts the session.

## Installation

Symlink into Pi's global extensions directory (edits then take effect live; use
`/reload` in a running session):

```bash
ln -sf "$(pwd)/extensions/codegraph.ts"        ~/.pi/agent/extensions/
ln -sf "$(pwd)/extensions/workflow-context.ts" ~/.pi/agent/extensions/
```

The MCP servers themselves are configured separately in `~/.pi/agent/mcp.json`
(see [Local Development Setup](../../docs/agent-memory.md#local-development-setup)).

## Not covered

Closing a **workflow** session on exit (witan's own Claude `Stop` hook) is not
mirrored: under Pi the session id differs from Claude's, so the `/tmp`
session-state file the checkpoint relies on isn't keyed the same way. Close
sessions explicitly with the `/witan-workflow end` skill, or rely on the next
`workflow_session_start`. This limitation is specific to the workflow-session
store — the **code-graph** `session_shutdown` handler above has no such
dependency and works the same as its Claude Stop-hook counterpart.
