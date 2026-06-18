# witan

A team-wide shared knowledge graph for coding agents, backed by
[Omnigraph](https://github.com/ModernRelay/omnigraph). Stores coding patterns,
project/repo facts, lessons, and agent context. Exposed over MCP so every
agent platform (pi, Claude Desktop, GitHub Copilot) can read and write without
platform-specific code.

## Quick Start

```bash
# 1. Install the omnigraph binary and initialise the local graph
./install.sh

# 2. Add the MCP server to your agent config (see config/ snippets)
#    pi:      merge config/pi.json into ~/.pi/agent/mcp.json
#    Claude:  merge config/claude.json into claude_desktop_config.json
#    Copilot: merge config/copilot.json into .vscode/mcp.json

# 3. Set your author name (optional — defaults to $USER)
export WITAN_AUTHOR="Your Name"
```

> **Wiring it into your agents locally** (both this server and witan-code,
> plus the hooks and skills, run straight from your checkout): see
> [Local Development Setup](../../../docs/agent-memory.md#local-development-setup).

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `WITAN_MEMORY_URI` | No | `~/.local/share/witan/graph.omni` | Graph URI — local path, `s3://`, or `http://` |
| `WITAN_MEMORY_TOKEN` | Only for `http://` | — | Bearer token for remote server auth |
| `WITAN_AUTHOR` | No | `$USER` | Attribution on every insert |
| `WITAN_REPO` | No | — | Repo slug override (bypasses git detection) |

## MCP Tools

### Memory Tools

| Tool | Description |
|---|---|
| `memory_search` | Full-text BM25 search across all memories, with optional `repo`/`kind` filters |
| `memory_store` | Insert a new memory (pattern, project_fact, lesson, or agent_context) |
| `memory_get` | Fetch a single memory by slug |
| `memory_get_project_facts` | Return all project facts for the current (or specified) repo |
| `memory_list_patterns` | List coding patterns, optionally scoped to a repo and/or language |

### Workflow Tracking Tools

Track engineering projects end-to-end across multiple Claude Code sessions.
See [`skills/workflow/project-tracker/SKILL.md`](../../../skills/workflow/project-tracker/SKILL.md) for usage.

| Tool | Description |
|---|---|
| `workflow_project_create` | Create a new project (`wp-` slug) to track an engineering objective |
| `workflow_project_get` | Fetch a single project by slug |
| `workflow_project_list` | List projects filtered by repo, status, or phase; defaults to active |
| `workflow_project_advance` | Advance a project to the next phase (discovery → spec → implementation → delivery) |
| `workflow_project_complete` | Mark project done; assembles a `WorkflowTrace` corpus record from all sessions |
| `workflow_session_start` | Link the current Claude Code session to a project; writes a state file for the Stop hook |
| `workflow_session_end` | Close the session with a summary, tools used, and files changed |

### Task Tracking Tools

A dependency-aware, hierarchical task tracker (beads-like) in the same graph, so
tasks hard-link to projects, sessions, and memories. See
[`skills/workflow/task-tracker/SKILL.md`](../../../skills/workflow/task-tracker/SKILL.md)
(the `/task` skill) for usage.

| Tool | Description |
|---|---|
| `task_create` | Create a task (`tk-` slug). Supports `parent` (epic → sub-issue), `blocked_by`, `project_slug`, `external_uri` (e.g. a GitHub issue), and `symbol_refs` |
| `task_get` | Fetch a single task by slug |
| `task_list` | List tasks filtered by repo, status, project, parent (epic children), or assignee |
| `task_update` | Update mutable fields — reassign, re-prioritise, re-parent, attach a URI (to *claim* for work, prefer `task_claim`) |
| `task_claim` | **Advisory claim** for parallel/multi-user agents: set `in_progress` under an `assignee` with a lease; refuses if actively held (use `force` to steal). Read-check-write, not an atomic lock — see `claimed_at` lease + the CAS follow-up |
| `task_release` | Drop a claim: clear assignee/lease, return the task to `open` |
| `task_close` | Close a task with an optional resolution; unblocks its dependents |
| `task_ready` | **Ready work**: open/unblocked tasks (plus `in_progress` tasks whose lease has lapsed) ordered by priority — the core coordination primitive |
| `task_link` | Link tasks: `blocks` / `parent` / `discovered_from`, or `addresses` a Memory node |
| `context_for_symbol` | Reverse lookup: given a code-graph symbol id (`repo#path::Name`), return the memories and tasks whose `symbolRefs` include it |

Tasks are **hierarchical** (an `epic` decomposes into sub-issues via `parent`, with
`parentSlug` denormalized for fast child lookup) and **dependency-aware** (`blocked_by`
maintains a denormalized `blockedBy` list that drives the `task_ready` query without
graph traversal). `external_uri` links a task to a GitHub issue/PR or any reference.

## Tests

Integration tests spin up throwaway omnigraph graphs and exercise the real query
files end-to-end (they skip automatically if the `omnigraph` binary is absent):

```bash
uv run --group test pytest
```

## Exploring the graph (`witan`)

A cyclopts CLI for manual inspection of the work-coordination graph and the
indexed code graphs. Installed alongside the server (`uv run witan …`,
or `uv tool install` the package to get it on `PATH`):

| Command | Shows |
|---|---|
| `tasks [--ready] [--status …] [--project wp-…] [--all-repos]` | Tasks for the current repo; `--ready` = open with no open blockers |
| `task <tk-slug>` | One task's details, blockers, and sub-tasks |
| `run <tk-slug> [--agent claude\|pi] [--dry-run]` | Claim a task and launch an agent to execute it |
| `projects [--status …] [--all-repos]` | Workflow projects (default: active in this repo) |
| `project <wp-slug>` | A project with its sessions, tasks, and corpus trace |
| `memory [QUERY] [--kind …]` | BM25 memory search, or the repo's project facts |
| `repos` | Repositories with a code graph indexed (files, size, freshness) |

`run` claims the task (`in_progress` + your author), then hands the terminal to
the agent seeded with the task description and a reminder to `task_close` when
done — invoke it from the task's repo checkout. Use `--dry-run` to print the
prompt without launching.

## Operating Modes

### Local Disk (default)

No extra infrastructure. Memories persist at
`~/.local/share/witan/graph.omni`.

```bash
# No env vars required.
export WITAN_AUTHOR="Alice Smith"
```

### Local RustFS (S3-compatible, for testing team mode)

```bash
RUSTFS=1 ./install.sh
# Follow the printed export instructions.
```

### Remote Team Server

```bash
export WITAN_MEMORY_URI=http://witan.internal:8080
export WITAN_MEMORY_TOKEN=<bearer-token>
export WITAN_AUTHOR="Alice Smith"
```

See [`docs/agent-memory.md`](../../../docs/agent-memory.md) for full
deployment instructions, the graph schema, and the v2 roadmap.

## Project Structure

```
witan/
├── README.md                  # This file
├── install.sh                 # Install omnigraph + init local graph
├── omnigraph.yaml             # CLI project config
├── pyproject.toml             # Python package metadata
├── schema/
│   └── schema.pg              # Omnigraph graph schema
├── queries/
│   ├── read.gq                # Read queries
│   └── mutations.gq           # Insert/update queries
├── witan/          # Python package
│   ├── __init__.py
│   ├── __main__.py            # Entry point
│   ├── server.py              # FastMCP app + tool definitions
│   ├── config.py              # Config loaded from env vars
│   ├── repo.py                # Git remote → canonical repo slug
│   └── graph.py               # OmnigraphClient (CLI subprocess wrapper)
└── config/
    ├── pi.json                # Snippet for ~/.pi/agent/mcp.json
    ├── claude.json            # Snippet for claude_desktop_config.json
    └── copilot.json           # Snippet for .vscode/mcp.json
```
