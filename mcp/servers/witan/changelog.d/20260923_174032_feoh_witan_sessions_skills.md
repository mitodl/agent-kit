### Fixed

- **Pi sessions now auto-close their WorkflowSession on shutdown.** The Pi
  workflow extension's `session_shutdown` handler ran `witan
  session-checkpoint`, but the checkpoint looked the parked session handle up
  by `$CLAUDE_SESSION_ID` alone, and the `witan-workflow` skill told Pi to
  invent a random session id, so under Pi the close never found anything. A
  new `session_state.current_session_id()` resolves the agent session id as an
  explicit value, else `$CLAUDE_SESSION_ID`, else `$PI_SESSION_ID`; the
  checkpoint, `witan session start`, the local-stdio provenance and task-claim
  holder fallbacks, and the remote proxy's injected `session_id`/`session_slug`
  all use it. Pi exposes `PI_SESSION_ID` only to its bash tool's commands, so
  the extension now sets it on the checkpoint child from
  `ctx.sessionManager.getSessionId()`, dropping any inherited
  `CLAUDE_SESSION_ID`/`PI_SESSION_ID` from that child only (also when Pi's id
  is unavailable, so the close no-ops rather than closing an enclosing
  session's handle), and the `witan-workflow`, `witan-project-tracker` and
  `witan-task` skills tell Pi agents to pass `$PI_SESSION_ID` as the
  `session_id`, reading only their own platform's variable rather than a
  `${CLAUDE_SESSION_ID:-$PI_SESSION_ID}` fallback that would pick an
  inherited Claude id. Claude Code behaviour is unchanged:
  `$CLAUDE_SESSION_ID` still wins whenever it is set.
