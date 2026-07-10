# Workflow & Code-Graph Hooks

Six Claude Code hooks that wire the trackers into every session: two
workflow hooks (bash scripts, `witan setup`-installed) and four code-graph
hooks (bare CLI commands — no scripts, portable to any platform installs on,
including Windows).

Both sets are installed automatically: `witan setup` for the workflow hooks
(and, when witan-code is also importable, the code-graph hooks too — as
`witan code <command>`, so only `witan` needs to be on `PATH`; see
[../../mcp/servers/witan/README.md#quick-start](../../mcp/servers/witan/README.md#quick-start)),
or `witan-code setup` on its own for a witan-code-only install (as
`witan-code <command>`; see
[../../mcp/servers/witan-code/README.md#install](../../mcp/servers/witan-code/README.md#install)).
The JSON below shows the standalone `witan-code …` form — substitute
`witan code …` if you registered them via `witan setup` instead. The manual
install below is for the two workflow hook *scripts* specifically, or a
full-repo-checkout dev loop; the code-graph hooks have no scripts to
symlink — register the bare commands directly (see below).

## Installation (workflow hooks)

```bash
mkdir -p ~/.claude/hooks
ln -sf "$(pwd)/workflow-context-inject.sh"     ~/.claude/hooks/
ln -sf "$(pwd)/workflow-session-checkpoint.sh" ~/.claude/hooks/
```

Run from the `configs/hooks/` directory, or adjust the symlink target to an
absolute path. The scripts resolve their own real location with `readlink -f`,
so the symlinked install correctly finds the package/query directories in the
repo (don't copy them — symlink, so edits take effect live).

## Hook Registration

Add a `hooks` key to your `~/.claude/settings.json`. The code-graph entries
(`witan-code …`) need `witan-code` on `PATH` (`uv tool install
git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan-code`,
or `witan-code setup` warns and points you at this if it's missing):

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
            "command": "witan-code inject-context",
            "timeout": 15
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
            "command": "witan-code checkpoint",
            "timeout": 15
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
            "command": "witan-code reindex-hook"
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
            "command": "witan-code session-init"
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

### `witan-code session-init` (SessionStart)

Runs once when a session starts. Seeds (first time) or refreshes the per-repo
Layer-2 code graph for the whole repository, so the index covers files the agent
never edits. Detaches a background child process and returns immediately, so
it never delays session start; the first run does the full build, later runs
re-hash and skip unchanged files. A per-repo lock (an atomic `mkdir` under
`$TMPDIR`, released by the detached child when it finishes) prevents
overlapping sessions from indexing at once. Skips non-git directories and
injects no context.

Together with `witan-code reindex-hook` (below) this makes the code graph
self-managing: SessionStart covers the whole repo, PostToolUse keeps live
edits fresh. Pi has no Claude-style hooks, but all four code-graph hooks
(including this one) are mirrored via its extension-events API — see
[configs/pi/README.md](../pi/README.md).

### `witan-code reindex-hook` (PostToolUse, matcher `Edit|Write`)

Runs after the agent edits a file. Reads the tool payload from stdin, and if the
changed file is a known source type (`.py`, `.ts`, `.tsx`, `.js`, …) incrementally
re-indexes just that file into the per-repo Layer-2 code graph, in the
foreground (a single file is fast). Best-effort and non-blocking: it always
exits 0 and prints nothing, so a missing/malformed payload or parse failure
never interrupts the agent.

### `witan-code inject-context` (UserPromptSubmit)

Runs before every prompt, independent of `workflow-context-inject.sh` (no
cross-package coupling — see `mcp/servers/witan-code/witan_code/graph.py`).
Reports whether the current repo has a Layer-2 code graph: if indexed, its
file count and last-updated time, plus a nudge to prefer `code_*` tools over
grep for symbol lookups, call graphs, and impact analysis; if `session-init`'s
background index is still running, says so instead of silently returning
partial/empty results. Prints nothing when the repo has neither a store nor
an index in flight.

### `witan-code checkpoint` (Stop)

Runs when Claude Code stops, independent of `workflow-session-checkpoint.sh`
(no cross-package coupling). Every witan-code write (the `SessionStart` full
index, the `PostToolUse` single-file reindex) appends a tiny Lance fragment +
manifest version, and an un-compacted store bloats until *opening* it
dominates query latency — the same failure mode witan's own store hit (#98).
This spawns a throttled, detached `witan-code optimize` (at most once per
`WITAN_CODE_OPTIMIZE_INTERVAL`, default daily; 0 disables) for the current
repo's store and the shared cross-repo bridge store, if either exists and is
due. Best-effort and non-blocking — always exits 0 and prints nothing, so a
missing binary or a bloated store taking tens of seconds to compact never
delays the Stop hook itself (the compaction runs detached).

## Environment Variables

The workflow hooks respect the same variables as the witan server;
the code-graph hooks use the witan-code variables:

| Variable | Default | Purpose |
|---|---|---|
| `WITAN_MEMORY_URI` | `~/.local/share/witan/graph.omni` | Graph location (workflow hooks) |
| `WITAN_MEMORY_TOKEN` | (empty) | Bearer token for http:// mode |
| `WITAN_CODE_DIR` | `~/.local/share/witan/code` | Per-repo code-store directory (code-graph hooks) |
| `WITAN_CODE_OPTIMIZE_INTERVAL` | `86400` (daily) | Throttle window in seconds for opportunistic store compaction; `0` disables |
| `CLAUDE_SESSION_ID` | (set by Claude Code) | Session UUID for state file keying |
| `CLAUDE_PROJECT_DIR` | `$(pwd)` | Project root for git remote detection and the code-graph lock |
