# witan

A team-wide shared knowledge graph for coding agents, backed by
[Omnigraph](https://github.com/ModernRelay/omnigraph). Stores coding patterns,
project/repo facts, lessons, and agent context. Exposed over MCP so every
agent platform (pi, Claude Desktop, GitHub Copilot) can read and write without
platform-specific code.

## Quick Start

`witan setup` installs the omnigraph binary, copies hooks/skills, and wires the
MCP server entry into your agent config in one step. Run it once; re-run after
upgrades to refresh installed files.

The omnigraph binary step always downloads the release pinned by
`_OMNIGRAPH_VERSION` in [`witan/setup.py`](./witan/setup.py) straight from
GitHub releases into `~/.local/bin/omnigraph` — there is no build-time
bundling, so re-running `witan setup` is also how you pick up an omnigraph
version bump (a Renovate PR bumps the pin; see `renovate.json`'s
`omnigraph-version` customManager). `witan-code`, if installed standalone
(without `witan`), has its own equivalent `witan-code setup` — see its
[README](../witan-code/README.md#install).

**With persistent CLI** — required for **Claude Code** and **Pi** (and `--agent all`),
whose hooks/extensions call the `witan` command directly, so it must stay on your `PATH`:

```bash
# Install the CLI from the published git repo …
uv tool install git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan
# … or editable from a local checkout, at the repo root:
uv tool install --editable mcp/servers/witan

# then run setup:
witan setup --agent claude   # or: pi | all
```

**Without persistent CLI** — enough for the **MCP-only agents** (Copilot, OpenCode,
Kilo), whose server runs via `uvx`, so no install is needed:

```bash
# From the published git repo …
uvx --from git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan \
    witan setup --agent copilot   # or: opencode | kilo
# … or from a local checkout, at the repo root:
uvx --from mcp/servers/witan witan setup --agent copilot
```

```bash
# Optional — set your author name (defaults to git config user.name or $USER):
export WITAN_AUTHOR="Your Name"
```

> **Manual wiring** (if you prefer to configure agents by hand): run
> `./install.sh` to initialise the local graph, then copy the appropriate
> config snippet from `config/` into your agent's MCP config file.

> **Local development setup** (both servers, hooks, and skills wired from your
> checkout): see
> [Local Development Setup](../../../docs/agent-memory.md#local-development-setup).

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `WITAN_MEMORY_URI` | No | `~/.local/share/witan/graph.omni` | Graph URI — local path, `s3://`, or `http://` |
| `WITAN_MEMORY_TOKEN` | Only for `http://` | — | Bearer token for remote server auth |
| `WITAN_AUTHOR` | No | `$USER` | Attribution on every insert |
| `WITAN_REPO` | No | — | Repo slug override (bypasses git detection) |
| `WITAN_SCAN_ENABLED` | No | `true` | Write-path secret/PII scanning; set to `false` to opt out — see [Write-path content scanning](docs/write-path-scanning.md) |

## MCP Tools

> **witan memory vs. your agent's built-in/session memory.** witan is the
> **shared, synced, team-wide** store; a coding agent's built-in memory (e.g.
> Claude Code's `memory/` files) is private to one machine and user. Durable,
> shareable engineering knowledge — project facts, patterns, lessons, decisions
> — belongs in witan so other agents and future sessions can find it; keep only
> private/ephemeral notes in built-in memory. The `witan-memory` skill and the
> MCP server instructions steer the agent accordingly.

### Memory Tools

| Tool | Description |
|---|---|
| `memory_search` | Full-text BM25 search across all memories, with optional `repo`/`kind` filters |
| `memory_list` | List memories (no search), optionally filtered by `kind` and/or `repo`; ordered most-recent first |
| `memory_store` | Insert a new memory (pattern, project_fact, lesson, or agent_context) |
| `memory_get` | Fetch a single memory by slug |
| `memory_get_project_facts` | Return all project facts for the current (or specified) repo |
| `memory_list_patterns` | List coding patterns, optionally scoped to a repo and/or language |

### Workflow Tracking Tools

Track engineering projects end-to-end across multiple Claude Code sessions.
See [`mcp/servers/witan/witan/skills/witan-project-tracker/SKILL.md`](./witan/skills/witan-project-tracker/SKILL.md) for usage.

| Tool | Description |
|---|---|
| `workflow_project_create` | Create a new project (`wp-` slug) to track an engineering objective |
| `workflow_project_get` | Fetch a single project by slug |
| `workflow_project_list` | List projects filtered by repo, status, or phase; defaults to active |
| `workflow_project_advance` | Advance a project to the next phase (discovery → spec → implementation → delivery) |
| `workflow_project_complete` | Mark project done; assembles a `WorkflowTrace` corpus record from all sessions |
| `workflow_project_link_memory` | Attach a memory (pattern/lesson/project_fact) to a project via an `Informed` edge |
| `workflow_project_block` | Declare that one project must complete before another can begin |
| `workflow_project_unblock` | Remove a project dependency declared with `workflow_project_block` |
| `workflow_project_get_blockers` | Return all projects currently blocking the given project |
| `workflow_session_start` | Link the current agent session to a project; writes a state file for the Stop hook |
| `workflow_session_end` | Close the session with a summary, tools used, and files changed |
| `workflow_trace_get` | Fetch a completed project's corpus `WorkflowTrace` by its `wt-`/`wp-` slug |

### Task Tracking Tools

A dependency-aware, hierarchical task tracker (beads-like) in the same graph, so
tasks hard-link to projects, sessions, and memories. See
[`mcp/servers/witan/witan/skills/witan-task/SKILL.md`](./witan/skills/witan-task/SKILL.md)
(the `/witan-task` skill) for usage.

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

### Code Branch Tracking

Links a git branch (repo + raw branch name, e.g. `feature/new-api` — never
witan-code's sanitized omnigraph branch name) to the task/project it's
carrying, so "which branch carries task X" and "which tasks are in flight on
branch B" are one-hop graph queries. Coordination state that lives in witan
(shared, durable), not witan-code's per-repo/bridge omnigraph stores (local,
re-derivable caches `branches --prune` may destroy at any time).

Wired in automatically, best-effort — no dedicated tool call needed:

- `workflow_session_start` upserts a `CodeBranch` for the current checkout's
  repo+branch (when detected) and links it `ForProject` to the session's
  project.
- `task_claim` upserts a `CodeBranch` on success and links it `WorksOn` the
  claimed task; re-calling `task_claim` (lease renewal) does not duplicate
  the edge.
- The `UserPromptSubmit` context-injection hook surfaces an **In-Flight
  Branch** section when the current checkout's branch already carries an
  open task — a signal that this session should likely continue that work
  rather than pick up something new.

Both call sites no-op silently with no repo/branch context (detached HEAD,
outside a git repo, or a store that predates this feature and hasn't run
`witan migrate schema` yet) — this is metadata riding alongside a task/
workflow tool call, never a hard requirement for the tool it's attached to.

## Write-path content scanning

Pluggable secret/PII scanning on every write (memories, tasks, projects,
sessions, traces), with block/redact/warn enforcement and a `witan.scanners`
entry-point mechanism so other organizations can add their own detection
rules without forking. Enabled by default (opt-out) — see
[`docs/write-path-scanning.md`](docs/write-path-scanning.md) for the full
config surface, the `witan scan test`/`witan scan rules` CLI, the audit
trail, and how to write a plugin, and
[ADR 0001](docs/adr/0001-write-path-content-scanning.md) for the design
rationale.

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

| Command | Description |
|---|---|
| `setup [--agent claude\|pi\|…\|all]` | Install witan for one or all supported coding agents; also writes a starter `~/.config/witan/config.toml` if one doesn't exist yet |
| `tasks [--ready] [--status …] [--project wp-…] [--all-repos]` | Tasks for the current repo (closed elided by default — pass `--status closed` to see them); `--ready` = open with no open blockers |
| `task <tk-slug>` | One task's details, blockers, and sub-tasks |
| `task create <title>` | Create a task from the CLI |
| `task close\|claim\|release\|update\|link <tk-slug> …` | Drive task state transitions from the CLI |
| `run <tk-slug> [--agent claude\|pi] [--dry-run]` | Claim a task and launch an agent to execute it |
| `projects [--status …] [--all-repos]` | Workflow projects (default: active in this repo) |
| `project <wp-slug>` | A project with its sessions, tasks, and corpus trace |
| `project status <wp-slug> [--json]` | "What next" resume view — phase, ready tasks, last session, blockers |
| `project tasks <wp-slug> [--status …] [--detail]` | A project's tasks; `--detail` expands each task's blockers and dependents |
| `project create <title>` | Create a workflow project from the CLI |
| `project advance\|complete\|block\|unblock <wp-slug> …` | Drive project phase/dependency transitions from the CLI |
| `session start\|end\|list …` | Start/close a workflow session, or list a project's sessions |
| `memory [QUERY] [--kind …]` | BM25 memory search, or (with no query) list memories |
| `code repos` | Repositories with a code graph indexed (requires witan-code) |
| `scan test <text>` | Dry-run active detectors against an ad-hoc string; prints findings (never the matched text) |
| `scan rules` | List active write-path scan detectors, their category, source, and enforcement mode |
| `inject-context [--debug]` | The UserPromptSubmit hook body; `--debug` prints detection/read diagnostics to stderr (repo, branch, graph reads, counts, swallowed-failure reasons) to explain a blank block |
| `serve` | Start the MCP server (memory + code tools when witan-code is installed) |

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
├── install.sh                 # Initialise local graph (manual alternative to `witan setup`)
├── pyproject.toml             # Python package metadata
├── schema/
│   └── schema.pg              # Omnigraph graph schema
├── queries/
│   ├── read.gq                # Read queries
│   └── mutations.gq           # Insert/update queries
├── tests/                     # Integration tests
├── witan/          # Python package
│   ├── __init__.py
│   ├── __main__.py            # Entry point
│   ├── server.py              # FastMCP app + tool definitions
│   ├── cli.py                 # `witan` umbrella CLI (cyclopts)
│   ├── config.py              # Config loaded from env vars
│   ├── context.py             # inject-context / session-checkpoint helpers
│   ├── repo.py                # Git remote → canonical repo slug
│   ├── graph.py               # OmnigraphClient (CLI subprocess wrapper)
│   ├── setup.py               # `witan setup` agent-installer + omnigraph fetch logic
│   ├── extensions/            # Agent extension configs (bundled by setup)
│   ├── hooks/                 # Shell hooks (bundled by setup)
│   └── skills/                # Bundled agent skills
└── config/
    ├── pi.json                # Snippet for ~/.pi/agent/mcp.json
    ├── claude.json            # Snippet for claude_desktop_config.json
    └── copilot.json           # Snippet for .vscode/mcp.json
```
