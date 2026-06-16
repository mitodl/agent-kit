# Workflow Tracking Hooks

Two Claude Code hooks that wire the workflow tracker into every session.

## Installation

```bash
mkdir -p ~/.claude/hooks
ln -sf "$(pwd)/workflow-context-inject.sh" ~/.claude/hooks/
ln -sf "$(pwd)/workflow-session-checkpoint.sh" ~/.claude/hooks/
```

Run from the `configs/hooks/` directory, or adjust the symlink target to an
absolute path.

## Hook Registration

Add a `hooks` key to your `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/workflow-context-inject.sh"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/workflow-session-checkpoint.sh"
          }
        ]
      }
    ]
  }
}
```

## What Each Hook Does

### `workflow-context-inject.sh` (UserPromptSubmit)

Runs before every prompt. Detects the current git repo, queries the
omnigraph-memory server for active `WorkflowProject` nodes, and injects
a context block listing them. This lets any session automatically discover
which project it should link to without the user providing that context.

Exits silently (no output) when there are no active projects for the repo,
so it adds no noise to untracked sessions.

### `workflow-session-checkpoint.sh` (Stop)

Runs when Claude Code stops. If `workflow_session_start` was called during
the session, a state file exists in `/tmp`. This hook reads it and calls
`update_workflow_session_end` with a placeholder summary so the session is
cleanly closed in the graph even if the agent forgot to call
`workflow_session_end` explicitly.

If `workflow_session_end` was already called, the state file is already gone
and this hook exits without doing anything.

## Environment Variables

Both hooks respect the same variables as the MCP server:

| Variable | Default | Purpose |
|---|---|---|
| `OMNIGRAPH_MEMORY_URI` | `~/.local/share/omnigraph-memory/graph.omni` | Graph location |
| `OMNIGRAPH_MEMORY_TOKEN` | (empty) | Bearer token for http:// mode |
| `CLAUDE_SESSION_ID` | (set by Claude Code) | Session UUID for state file keying |
| `CLAUDE_PROJECT_DIR` | `$(pwd)` | Project root for git remote detection |
