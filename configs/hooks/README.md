# Workflow & Code-Graph Hooks

Four Claude Code hooks that wire the trackers into every session.

## Installation

```bash
mkdir -p ~/.claude/hooks
ln -sf "$(pwd)/workflow-context-inject.sh"     ~/.claude/hooks/
ln -sf "$(pwd)/workflow-session-checkpoint.sh" ~/.claude/hooks/
ln -sf "$(pwd)/codegraph-session-init.sh"      ~/.claude/hooks/
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
    ],
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/codegraph-session-init.sh"
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
witan server for active `WorkflowProject` nodes, and injects
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

### `codegraph-session-init.sh` (SessionStart)

Runs once when a session starts. Seeds (first time) or refreshes the per-repo
Layer-2 code graph for the whole repository, so the index covers files the agent
never edits. It runs the indexer **detached in the background** and returns
immediately, so it never delays session start; the first run does the full build,
later runs re-hash and skip unchanged files. A per-repo lock prevents overlapping
sessions from indexing at once. Skips non-git directories and injects no context.

Together with `codegraph-reindex.sh` (below) this makes the code graph
self-managing: SessionStart covers the whole repo, PostToolUse keeps live edits
fresh. (Pi has no hook system, so under Pi the initial seed stays manual —
`witan-code index .`.)

### `codegraph-reindex.sh` (PostToolUse, matcher `Edit|Write`)

Runs after the agent edits a file. Reads the tool payload from stdin, and if the
changed file is a known source type (`.py`, `.ts`, `.tsx`, `.js`, …) incrementally
re-indexes just that file into the per-repo Layer-2 code graph. Best-effort and
non-blocking: it always exits 0 and silences all output, so a missing binary,
missing package, or parse failure never interrupts the agent.

Prefers the `witan-code` CLI on `PATH`
(`uv tool install --editable mcp/servers/witan-code`); otherwise falls
back to `uvx --from <local package>`.

## Environment Variables

The workflow hooks respect the same variables as the witan server;
the codegraph hook uses the witan-code variables:

| Variable | Default | Purpose |
|---|---|---|
| `WITAN_MEMORY_URI` | `~/.local/share/witan/graph.omni` | Graph location (workflow hooks) |
| `WITAN_MEMORY_TOKEN` | (empty) | Bearer token for http:// mode |
| `WITAN_CODE_DIR` | `~/.local/share/witan/code` | Per-repo code-store directory (codegraph hook) |
| `CLAUDE_SESSION_ID` | (set by Claude Code) | Session UUID for state file keying |
| `CLAUDE_PROJECT_DIR` | `$(pwd)` | Project root for git remote detection |
