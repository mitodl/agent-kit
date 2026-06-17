# Workflow & Code-Graph Hooks

Three Claude Code hooks that wire the trackers into every session.

## Installation

```bash
mkdir -p ~/.claude/hooks
ln -sf "$(pwd)/workflow-context-inject.sh"     ~/.claude/hooks/
ln -sf "$(pwd)/workflow-session-checkpoint.sh" ~/.claude/hooks/
ln -sf "$(pwd)/codegraph-reindex.sh"           ~/.claude/hooks/
```

Run from the `configs/hooks/` directory, or adjust the symlink target to an
absolute path. The scripts resolve their own real location with `readlink -f`,
so the symlinked install correctly finds the package/query directories in the
repo (don't copy them — symlink, so edits take effect live).

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
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/codegraph-reindex.sh"
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

### `codegraph-reindex.sh` (PostToolUse, matcher `Edit|Write`)

Runs after the agent edits a file. Reads the tool payload from stdin, and if the
changed file is a known source type (`.py`, `.ts`, `.tsx`, `.js`, …) incrementally
re-indexes just that file into the per-repo Layer-2 code graph. Best-effort and
non-blocking: it always exits 0 and silences all output, so a missing binary,
missing package, or parse failure never interrupts the agent.

Prefers the `omnigraph-codegraph-index` CLI on `PATH`
(`uv tool install --editable mcp/servers/omnigraph-codegraph`); otherwise falls
back to `uvx --from <local package>`.

## Environment Variables

The workflow hooks respect the same variables as the omnigraph-memory server;
the codegraph hook uses the omnigraph-codegraph variables:

| Variable | Default | Purpose |
|---|---|---|
| `OMNIGRAPH_MEMORY_URI` | `~/.local/share/omnigraph-memory/graph.omni` | Graph location (workflow hooks) |
| `OMNIGRAPH_MEMORY_TOKEN` | (empty) | Bearer token for http:// mode |
| `OMNIGRAPH_CODEGRAPH_DIR` | `~/.local/share/omnigraph-memory/code` | Per-repo code-store directory (codegraph hook) |
| `CLAUDE_SESSION_ID` | (set by Claude Code) | Session UUID for state file keying |
| `CLAUDE_PROJECT_DIR` | `$(pwd)` | Project root for git remote detection |
