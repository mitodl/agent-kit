# Pi Extensions

Pi has no Claude-style hooks, but its [extension events](https://pi.dev/docs/latest/extensions#events)
provide the equivalent triggers. These extensions mirror the Claude Code hooks
so the omnigraph trackers work the same under Pi.

| Extension | Pi events | Mirrors Claude hook |
|---|---|---|
| `extensions/codegraph.ts` | `session_start`, `tool_call`/`tool_result` (`edit`/`write`) | codegraph-session-init + codegraph-reindex |
| `extensions/workflow-context.ts` | `before_agent_start` | workflow-context-inject |

- **codegraph.ts** — on session start, seeds/refreshes the whole repo's Layer-2
  code graph in the background; after each `edit`/`write`, re-indexes that file.
  Requires `witan-code` on `PATH`
  (`uv tool install --editable mcp/servers/witan-code`); otherwise no-ops.
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

Closing a workflow session on exit (the Claude `Stop` hook) is not mirrored:
under Pi the session id differs from Claude's, so the `/tmp` session-state file
the checkpoint relies on isn't keyed the same way. Close sessions explicitly with
the `/witan-workflow end` skill, or rely on the next `workflow_session_start`.
