# Workflow & Code-Graph Hooks

Six Claude Code hooks that wire the trackers into every session.

The four codegraph hooks below are also installed automatically by
`witan-code setup` (see
[../../mcp/servers/witan-code/README.md#install](../../mcp/servers/witan-code/README.md#install)),
which additionally registers the MCP server and skill in one step. The manual
symlink install below is for the two workflow hooks (installed via
`witan setup`) or a full-repo-checkout dev loop.

## Installation

```bash
mkdir -p ~/.claude/hooks
ln -sf "$(pwd)/workflow-context-inject.sh"     ~/.claude/hooks/
ln -sf "$(pwd)/workflow-session-checkpoint.sh" ~/.claude/hooks/
ln -sf "$(pwd)/codegraph-session-init.sh"      ~/.claude/hooks/
ln -sf "$(pwd)/codegraph-reindex.sh"           ~/.claude/hooks/
ln -sf "$(pwd)/codegraph-context.sh"           ~/.claude/hooks/
ln -sf "$(pwd)/codegraph-checkpoint.sh"        ~/.claude/hooks/
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
          },
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/codegraph-context.sh"
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
          },
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/codegraph-checkpoint.sh"
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
fresh. Pi has no Claude-style hooks, but all four codegraph hooks (including
this one) are mirrored via its extension-events API — see
[configs/pi/README.md](../pi/README.md).

### `codegraph-reindex.sh` (PostToolUse, matcher `Edit|Write`)

Runs after the agent edits a file. Reads the tool payload from stdin, and if the
changed file is a known source type (`.py`, `.ts`, `.tsx`, `.js`, …) incrementally
re-indexes just that file into the per-repo Layer-2 code graph. Best-effort and
non-blocking: it always exits 0 and silences all output, so a missing binary,
missing package, or parse failure never interrupts the agent.

Prefers the `witan-code` CLI on `PATH`
(`uv tool install --editable mcp/servers/witan-code`); otherwise falls
back to `uvx --from <local package>`.

### `codegraph-context.sh` (UserPromptSubmit)

Runs before every prompt, independent of `workflow-context-inject.sh` (no
cross-package coupling — see `mcp/servers/witan-code/witan_code/graph.py`).
Reports whether the current repo has a Layer-2 code graph: if indexed, its
file count and last-updated time, plus a nudge to prefer `code_*` tools over
grep for symbol lookups, call graphs, and impact analysis; if
`codegraph-session-init.sh`'s background index is still running, says so
instead of silently returning partial/empty results. Exits silently when the
repo has neither a store nor an index in flight.

### `codegraph-checkpoint.sh` (Stop)

Runs when Claude Code stops, independent of `workflow-session-checkpoint.sh`
(no cross-package coupling). Every witan-code write (the `SessionStart` full
index, the `PostToolUse` single-file reindex) appends a tiny Lance fragment +
manifest version, and an un-compacted store bloats until *opening* it
dominates query latency — the same failure mode witan's own store hit (#98).
This spawns a throttled, detached `witan-code optimize` (at most once per
`WITAN_CODE_OPTIMIZE_INTERVAL`, default daily; 0 disables) for the current
repo's store and the shared cross-repo bridge store, if either exists and is
due. Best-effort and non-blocking — always exits 0 and silences all output,
so a missing binary or a bloated store taking tens of seconds to compact
never delays the Stop hook itself (the compaction runs detached).

## Environment Variables

The workflow hooks respect the same variables as the witan server;
the codegraph hook uses the witan-code variables:

| Variable | Default | Purpose |
|---|---|---|
| `WITAN_MEMORY_URI` | `~/.local/share/witan/graph.omni` | Graph location (workflow hooks) |
| `WITAN_MEMORY_TOKEN` | (empty) | Bearer token for http:// mode |
| `WITAN_CODE_DIR` | `~/.local/share/witan/code` | Per-repo code-store directory (codegraph hook) |
| `WITAN_CODE_OPTIMIZE_INTERVAL` | `86400` (daily) | Throttle window in seconds for opportunistic store compaction; `0` disables |
| `CLAUDE_SESSION_ID` | (set by Claude Code) | Session UUID for state file keying |
| `CLAUDE_PROJECT_DIR` | `$(pwd)` | Project root for git remote detection |
