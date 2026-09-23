# Pi Extensions

Pi has no Claude-style hooks, but its [extension events](https://pi.dev/docs/latest/extensions#events)
provide the equivalent triggers. These extensions mirror the Claude Code hooks
so the omnigraph trackers work the same under Pi.

| Extension | Pi events | Mirrors Claude hook(s) |
|---|---|---|
| `extensions/codegraph.ts` | `session_start`, `tool_call`/`tool_result` (`edit`/`write`), `before_agent_start`, `session_shutdown` | codegraph-session-init + codegraph-reindex + codegraph-context + codegraph-checkpoint |
| `extensions/workflow-context.ts` | `before_agent_start`, `session_shutdown` | workflow-context-inject + workflow-session-checkpoint |

- **codegraph.ts** (owned by witan-code; see
  [mcp/servers/witan-code/witan_code/extensions/pi/codegraph.ts](../../mcp/servers/witan-code/witan_code/extensions/pi/codegraph.ts),
  installed by `witan-code setup --agent pi`) —
  - `session_start`: seeds/refreshes the whole repo's Layer-2 code graph in the background.
  - `tool_call`/`tool_result` (`edit`/`write`): incrementally re-indexes the edited file.
  - `before_agent_start`: reports whether the code graph is indexed (file count,
    last-updated) or still being built, plus cross-repo coverage and the
    `ToolSearch` + `code_find_definition` calls to make against it.
  - `session_shutdown`: opportunistically compacts the current repo's store and
    the shared bridge store (throttled) — has no session-id dependency, so it
    *is* mirrored under Pi.

  Requires `witan-code` on `PATH` (`witan-code setup --agent pi`, or
  `uv tool install --editable mcp/servers/witan-code`); otherwise no-ops.
- **workflow-context.ts** (owned by witan; see
  [mcp/servers/witan/witan/extensions/pi/workflow-context.ts](../../mcp/servers/witan/witan/extensions/pi/workflow-context.ts),
  installed by `witan setup --agent pi`) —
  - `before_agent_start`: appends the repo's active WorkflowProjects and ready
    tasks to the system prompt. Requires the `omnigraph` binary and the witan
    graph.
  - `session_shutdown`: runs `witan session-checkpoint`, whose optimize half
    (`spawn_background_optimize`) opportunistically compacts the memory graph
    store (throttled; see #124) so query latency doesn't re-bloat, and
    auto-closes the active WorkflowSession. The close looks the session handle
    up by the agent session id: Pi exposes `PI_SESSION_ID` only to commands its
    `bash` tool runs, not to its own process env, so the extension sets it on
    the checkpoint child from `ctx.sessionManager.getSessionId()` (and drops any
    inherited `CLAUDE_SESSION_ID` from that child only, since witan prefers it).
    For the close to find the session, `workflow_session_start` must have been
    given the same id — the `witan-workflow` and `witan-project-tracker` skills
    tell the agent to pass `$PI_SESSION_ID`. With no handle the close no-ops.

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
(see [Local Development Setup](../../docs/internals/agent-memory.md#local-development-setup)).
