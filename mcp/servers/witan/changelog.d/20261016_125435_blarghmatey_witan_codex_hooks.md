### Fixed

- `witan session-checkpoint` closes the session under Codex. Codex runs its Stop hook with no session variable in the environment, so the checkpoint would find no handle and leave the session open. It now takes the id from the hook's stdin payload when the payload is Codex's (it carries `turn_id`); a Claude Code payload is not read, so the Claude path is unchanged.
- `witan session-checkpoint` sends a half-configured remote's error to stderr. It went to stdout, which Codex reads as a malformed Stop decision and reports as a failed hook.
- The Pi extension's shutdown checkpoint drops an inherited `$CODEX_SESSION_ID` from its child, as it already did `$CLAUDE_SESSION_ID`. A Pi started from Codex's shell with no session id of its own would otherwise close the Codex session's witan session.

### Added

- `$CODEX_SESSION_ID`, which Codex sets in every command its shell tool runs, is the last fallback for the agent session id, after `$CLAUDE_SESSION_ID` and `$PI_SESSION_ID`. It is the id Codex hands its hooks, so `witan session start` run from Codex parks the handle where the Stop hook looks. The witan-workflow and witan-project-tracker skills tell a Codex agent to pass it to `workflow_session_start`.
