# omnigraph-memory

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
export OMNIGRAPH_MEMORY_AUTHOR="Your Name"
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OMNIGRAPH_MEMORY_URI` | No | `~/.local/share/omnigraph-memory/graph.omni` | Graph URI — local path, `s3://`, or `http://` |
| `OMNIGRAPH_MEMORY_TOKEN` | Only for `http://` | — | Bearer token for remote server auth |
| `OMNIGRAPH_MEMORY_AUTHOR` | No | `$USER` | Attribution on every insert |
| `OMNIGRAPH_MEMORY_REPO` | No | — | Repo slug override (bypasses git detection) |

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

## Operating Modes

### Local Disk (default)

No extra infrastructure. Memories persist at
`~/.local/share/omnigraph-memory/graph.omni`.

```bash
# No env vars required.
export OMNIGRAPH_MEMORY_AUTHOR="Alice Smith"
```

### Local RustFS (S3-compatible, for testing team mode)

```bash
RUSTFS=1 ./install.sh
# Follow the printed export instructions.
```

### Remote Team Server

```bash
export OMNIGRAPH_MEMORY_URI=http://omnigraph-memory.internal:8080
export OMNIGRAPH_MEMORY_TOKEN=<bearer-token>
export OMNIGRAPH_MEMORY_AUTHOR="Alice Smith"
```

See [`docs/agent-memory.md`](../../../docs/agent-memory.md) for full
deployment instructions, the graph schema, and the v2 roadmap.

## Project Structure

```
omnigraph-memory/
├── README.md                  # This file
├── install.sh                 # Install omnigraph + init local graph
├── omnigraph.yaml             # CLI project config
├── pyproject.toml             # Python package metadata
├── schema/
│   └── schema.pg              # Omnigraph graph schema
├── queries/
│   ├── read.gq                # Read queries
│   └── mutations.gq           # Insert/update queries
├── omnigraph_memory/          # Python package
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
