# CLI Reference

Exhaustive reference for the `witan` command (cyclopts, entry point
`witan.cli:main`). Generated from the actual `@app.command` definitions in
`witan/cli/*.py` — run `witan <command> --help` (or `witan <group> <command>
--help`) for the live, authoritative version; this doc mirrors it at time of
writing.

Every list-typed option (e.g. `--tags`, `--blocked-by`) accepts a
comma-separated string or is repeatable; cyclopts also generates a paired
`--empty-<flag>` to explicitly pass an empty list. Every boolean option is
generated as a `--flag/--no-flag` pair; the table below shows only the
`--flag` spelling with its default.

Global conventions used throughout:

- **Repo scoping.** Most list/query commands take `--repo <uri>` (defaults to
  the git repo detected from `origin` in the current checkout, or `WITAN_REPO`)
  and `--all-repos` (span every repo in the graph).
- **Config resolution.** `--target`, `--agent`, `--model` on `run`-family
  commands resolve env var > named `[targets.*]` in `config.toml` > global
  `config.toml` value > hardcoded default (`agent="claude"`). See
  `witan/config.py:load()`.

## Top-level commands

| Command | Description |
|---|---|
| `serve` | Run the witan MCP server |
| `run` | Claim a task and launch an agent to execute it |
| `setup` | Install witan for one or all supported coding agents |
| `tasks` | List tasks (see [Tasks](#tasks)) |
| `task` | Manage a single task (see [Tasks](#tasks)) |
| `projects` | List workflow projects (see [Projects](#projects)) |
| `project` | Manage a single workflow project (see [Projects](#projects)) |
| `memory` | Search or list memories (see [Memory](#memory)) |
| `traces` | List corpus workflow traces (see [Traces](#traces)) |
| `trace` | Inspect a single corpus trace (see [Traces](#traces)) |
| `graph` | Visualize the project/task dependency graph (see [Graph](#graph)) |
| `scan` | Introspect write-path content scanning (see [Scan](#scan)) |
| `migrate` | One-shot schema/data migrations (see [Migrate](#migrate)) |
| `inject-context` | Hook helper — print workflow context (see [Hooks](#hooks)) |
| `session-checkpoint` | Hook helper — close the active session (see [Hooks](#hooks)) |
| `code` | Code-graph commands, mounted only if `witan-code` is installed (see [Code](#code-witan-code-only)) |

---

## `serve`

Run the witan MCP server. Serves `memory_*`/`task_*`/`workflow_*` tools and,
when `witan-code` is installed, mounts its `code_*` tools into the same
server.

| Flag | Type | Default | Env var | Description |
|---|---|---|---|---|
| `--transport` | `stdio\|http\|streamable-http\|sse` | `stdio` | `WITAN_MCP_TRANSPORT` | MCP transport. `stdio` for local (Claude Desktop, `uvx`); the others bind a network listener |
| `--host` | str | `127.0.0.1` | `WITAN_MCP_HOST` | Interface to bind for HTTP transports (`0.0.0.0` inside a container) |
| `--port` | int | `8000` | `WITAN_MCP_PORT` | Port to bind for HTTP transports |
| `--path` | str | `/mcp` | `WITAN_MCP_PATH` | URL path the MCP endpoint is served on (HTTP transports only) |

```bash
witan serve --transport streamable-http --host 0.0.0.0 --port 8080
```

## `setup`

Install witan for one or all supported coding agents: downloads the
`omnigraph` binary to `~/.local/bin/`, writes a starter `config.toml` if
none exists, copies bundled skills/hooks/extensions, and merges the MCP
server entry into the agent's config. Safe to re-run after upgrades.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--agent` | `claude\|pi\|copilot\|opencode\|all` | `claude` | Target agent (Kilo Code is pending a config-path fix, tracked separately) |
| `--author` | str \| None | git `user.name`, then `$USER` | Name written to graph nodes |
| `--dry-run` | bool | `False` | Print what would happen without writing anything |

```bash
witan setup --agent claude --author "Alice Smith"
```

## `run`

Claim a task and launch an agent to execute it. Claims the task
(`in_progress`, assignee = your author), then hands the terminal to `--agent`
seeded with a prompt built from the task's description. Run this from the
task's repo checkout.

| Flag | Type | Default | Description |
|---|---|---|---|
| `SLUG` (positional) | str | required | Task slug (`tk-…`) |
| `--target` | str \| None | — | Named config target; also `WITAN_TARGET` |
| `--agent` | str \| None | config/target default | Agent CLI to launch (`claude`, `pi`, `copilot`, `opencode`, `kilo`); also `WITAN_AGENT` |
| `--model` | str \| None | config/target default | Model passed to the agent's `--model` flag; also `WITAN_MODEL` |
| `--claim` | bool | `True` | Mark the task `in_progress` and assign it to you first |
| `--dry-run` | bool | `False` | Print the prompt and exit without launching or claiming |

```bash
witan run tk-fix-flaky-retry-abc123 --dry-run
```

## Tasks

### `tasks` — list

List tasks for the current repo (or filtered).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--repo` | str \| None | detected repo | Scope to a specific repo URI |
| `--status` | str \| None | — | Filter: `open`, `in_progress`, `blocked`, `closed` |
| `--project` | str \| None | — | Scope to a `wp-` WorkflowProject slug |
| `--assignee` | str \| None | — | Filter by owner |
| `--ready` | bool | `False` | Show only ready-to-work tasks (open, all blockers closed) |
| `--all-repos` | bool | `False` | Span every repo in the graph |
| `--limit` | int | `50` | Max rows |

```bash
witan tasks --ready --project wp-migrate-auth-oauth2
```

### `task <slug>` — show

Default subcommand of the `task` group: `witan task tk-…` prints one task's
details, its blockers, and its sub-tasks. No dedicated flags.

```bash
witan task tk-fix-flaky-retry-abc123
```

### `task create` — create

Create a task in the work-coordination graph.

| Flag | Type | Default | Description |
|---|---|---|---|
| `TITLE` (positional) | str | required | Short label for the work |
| `--description` | str | `""` | Full description |
| `--type` | `bug\|feature\|task\|chore\|epic` | `task` | Task type |
| `--priority` | `p0\|p1\|p2\|p3` | `p2` | Priority (p0 highest) |
| `--repo` | str \| None | detected repo | Repo URI |
| `--project` | str \| None | — | `wp-` slug this task rolls up to |
| `--parent` | str \| None | — | `tk-` slug of the parent task/epic |
| `--blocked-by` | list[str] \| None | — | `tk-` slugs that must close before this is ready |
| `--discovered-from` | list[str] \| None | — | `tk-` slugs of tasks this work was discovered during |
| `--external-uri` | str \| None | — | Reference URI (GitHub issue/PR, etc.) |
| `--symbol-refs` | list[str] \| None | — | Code-graph symbol ids (`repo#path::Name`) |
| `--tags` | list[str] \| None | — | Free-form tags |

```bash
witan task create "Fix flaky retry test" --type bug --priority p1 --blocked-by tk-abc123
```

### `task run` — claim and launch

Claim one or more tasks and launch an agent to execute them. Without a slug,
shows an interactive picker of ready tasks; multiple selections offer a
consolidated single-session prompt or sequential per-task runs.

| Flag | Type | Default | Description |
|---|---|---|---|
| `SLUG` (positional) | str \| None | — | Task slug to run directly (skips the picker) |
| `--target` | str \| None | — | Named config target |
| `--agent` | str \| None | config/target default | Agent CLI to launch |
| `--model` | str \| None | config/target default | Model flag passed to the agent |
| `--claim` | bool | `True` | Mark each task `in_progress` before launching |
| `--dry-run` | bool | `False` | Print the prompt(s) without launching or claiming |
| `--repo` | str \| None | detected repo | Scope the picker to a specific repo |
| `--all-repos` | bool | `False` | Span all repos in the picker |
| `--project` | str \| None | — | Scope the picker to a `wp-` project slug |

```bash
witan task run --project wp-migrate-auth-oauth2
```

## Projects

### `projects` — list

List workflow projects (default: active in the current repo).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--repo` | str \| None | detected repo | Scope to a specific repo URI |
| `--status` | str \| None | `active` | Status filter |
| `--all-repos` | bool | `False` | Span every repo |
| `--limit` | int | `50` | Max rows |

```bash
witan projects --status active
```

### `project <slug>` — show

Default subcommand of the `project` group: shows a project with its
sessions, rolled-up tasks, and (once completed) its corpus trace summary. No
dedicated flags.

```bash
witan project wp-migrate-auth-oauth2
```

### `project create` — create

Create a new workflow project.

| Flag | Type | Default | Description |
|---|---|---|---|
| `TITLE` (positional) | str | required | Short project name |
| `--description` | str | `""` | Full objective description |
| `--phase` | `discovery\|spec\|implementation\|delivery` | `discovery` | Starting phase |
| `--repo` | str \| None | detected repo | Repo URI to associate |
| `--github-issue` | str \| None | — | URL of the tracking GitHub issue |
| `--tags` | list[str] \| None | — | Tags for grouping/search |

```bash
witan project create "Migrate auth to OAuth2" --phase discovery
```

### `project run` — launch an agent on a project

Launch an agent session focused on a workflow project. Without a slug, shows
an interactive picker of active projects; multiple selections offer a
consolidated or sequential run.

| Flag | Type | Default | Description |
|---|---|---|---|
| `SLUG` (positional) | str \| None | — | Project slug to run directly (skips the picker) |
| `--target` | str \| None | — | Named config target |
| `--agent` | str \| None | config/target default | Agent CLI to launch |
| `--model` | str \| None | config/target default | Model flag |
| `--dry-run` | bool | `False` | Print the prompt(s) without launching |
| `--repo` | str \| None | detected repo | Scope the picker to a specific repo |
| `--all-repos` | bool | `False` | Span all repos in the picker |

```bash
witan project run wp-migrate-auth-oauth2 --dry-run
```

## Memory

### `memory [QUERY]`

Search memory (BM25), or with no query list memories (filtered by `--kind`).

| Flag | Type | Default | Description |
|---|---|---|---|
| `QUERY` (positional) | str \| None | — | Search text; omit to list instead of search |
| `--kind` | `pattern\|project_fact\|lesson\|agent_context` | — | Filter by memory kind |
| `--repo` | str \| None | detected repo | Scope to a repo |
| `--all-repos` | bool | `False` | Span every repo |
| `--limit` | int | `20` | Max rows |

```bash
witan memory "flaky retry" --kind pattern
```

## Traces

WorkflowTrace records are corpus material assembled by
`workflow_project_complete` — every session that went into a completed
project, plus mined lessons/patterns.

### `traces` — list

List corpus workflow traces (default: current repo).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--repo` | str \| None | detected repo | Scope to a repo |
| `--tags` | list[str] \| None | — | Filter by tag |
| `--author` | str \| None | — | Filter by author |
| `--all-repos` | bool | `False` | Span every repo |
| `--limit` | int | `50` | Max rows |

```bash
witan traces --repo https://github.com/mitodl/agent-kit
```

### `trace <slug>` — show

Default subcommand of the `trace` group: prints a trace's outcome narrative,
its sessions, and its mined patterns/lessons in full (not just slugs). No
dedicated flags.

```bash
witan trace wt-migrate-auth-oauth2
```

### `trace list`

Alias of `witan traces` (same flags as above), reachable as a `trace`
subcommand for symmetry with `task`/`project`.

## Graph

Visualize the workflow project and task dependency graph: prints a Rich
summary, and optionally writes an interactive HTML graph (vis-network) or a
Graphviz DOT file.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--repo` | str \| None | detected repo | Scope to a specific repo |
| `--all-repos` | bool | `False` | Include projects/tasks from every repo |
| `--status` | str \| None | `active` | Project status filter (`active\|completed\|abandoned`); pass `""` for all |
| `--all-tasks` | bool | `False` | Include closed tasks (default: open + in_progress + blocked only) |
| `--no-belongs-to` | bool | `False` | Omit dashed task→project edges to reduce clutter |
| `--html` | Path \| None | — | Write a self-contained interactive HTML graph to this path |
| `--dot` | Path \| None | — | Write a Graphviz DOT file to this path |
| `--open-browser` | bool | `False` | Open the generated HTML in the default browser (requires `--html`) |

```bash
witan graph --html /tmp/witan-graph.html --open-browser
```

## Scan

Introspect and dry-run write-path content scanning (secret/PII detection —
see [`docs/write-path-scanning.md`](write-path-scanning.md)).

### `scan test TEXT`

Dry-run active detectors against `TEXT` and print findings. Nothing is
written — this exercises the same `ScannerRegistry` the write path uses, and
works even with `WITAN_SCAN_ENABLED=false`.

| Flag | Type | Default | Description |
|---|---|---|---|
| `TEXT` (positional) | str | required | The string to scan |
| `--field` | str | `content` | Field name to report context for (some detectors are field-aware, e.g. skipping `author`) |
| `--node-type` | str | `Memory` | Node type to report context for (some detectors are node-aware) |

```bash
witan scan test "my email is a@b.com, key AKIAIOSFODNN7EXAMPLE"
```

### `scan rules`

List active detectors: category, source (`built-in`, `entry-point:<name>`,
or `config:<dotted.path>`), and resolved enforcement mode. No flags.

```bash
witan scan rules
```

## Migrate

One-shot, idempotent schema and data migrations, mounted as a sub-app
(`witan migrate <subcommand>`).

### `migrate schema`

Apply the bundled schema to the configured store (idempotent). Reconciles an
existing store with the current schema — new nodes/edges/fields added since
it was first created. No flags.

```bash
witan migrate schema
```

### `migrate storage [OLD_BINARY]`

Rebuild a local store stuck on an old, incompatible omnigraph on-disk format
(omnigraph refuses to open a store written by a newer/older binary's
internal schema version). Exports with the old binary, then `init` + `load`
with the current one. Preserves nodes/edges/vectors/blobs; commit history
and branches are not preserved. Original store is renamed
`<store>.pre-migrate`, not deleted. No-op if the store already opens fine.
Only handles local on-disk stores — `s3://`/`http(s)://` stores must be
rebuilt by hand.

| Flag | Type | Default | Description |
|---|---|---|---|
| `OLD_BINARY` (positional) | str \| None | auto-detected | Path to the omnigraph binary that last wrote this store (first `omnigraph` on `PATH` that isn't the current one, if omitted) |
| `--yes` | bool | `False` | Skip the confirmation prompt |

```bash
witan migrate storage --yes
```

### `migrate topics`

Backfill `Topic` nodes from existing memory tags: for every distinct memory
tag, upsert a `Topic{kind:"topic"}` and a `Tagged` edge. Safe to re-run
(already-created topics/edges are skipped). Fails fast if the Topic schema
isn't applied yet (`migrate schema` first). No flags.

```bash
witan migrate topics
```

### `migrate all`

Run the full bring-up: `migrate schema` then `migrate topics`. Both steps
are idempotent. No flags.

```bash
witan migrate all
```

## Hooks

Internal commands invoked by bundled agent hooks, not typically run by hand.
Both always exit 0 and never block, even when the graph is missing or the
directory isn't a git repo.

### `inject-context`

Print workflow context (active WorkflowProjects, ready Tasks) for the
current git repo to stdout, for the `UserPromptSubmit` hook
(`~/.claude/hooks/workflow-context-inject.sh`). No flags.

### `session-checkpoint`

Auto-close the active WorkflowSession on agent stop: reads the state file
written by `workflow_session_start` and records an end timestamp. No-op if
that file is absent (Stop hook). No flags.

## `code` (witan-code only)

Mounted as `witan code …` only when the sibling `witan-code` package is
installed (falls back silently otherwise — the umbrella CLI works standalone).
These commands come from `witan_code/cli.py`; see
[`../witan-code/README.md`](../../witan-code/README.md) for the full
code-graph documentation. Listed here for completeness since they're part of
the same CLI surface once `witan-code` is present.

| Command | Flags | Description |
|---|---|---|
| `code index [PATH]` | `PATH` (positional, default `.`) | Incrementally index PATH (file or directory); unchanged files skipped |
| `code reindex [PATH]` | `PATH` (positional, default `.`) | Force re-index PATH, ignoring content hashes |
| `code deps` | `--kind`, `--repo`, `--html`, `--open-browser`, `--min-precision` (`precise\|heuristic\|fuzzy`, default `heuristic`) | Visualize cross-repo dependencies from the shared bridge store |
| `code symbols` | `--repo`, `--role` (`exported\|external`), `--scheme` | Print a repo's symbol table from the bridge store |
| `code stitch [REPO]` | `REPO` (positional), `--unresolved` | Print Stage-2 precise cross-repo edges (or unresolved external references) |
| `code branches` | `--prune` | List omnigraph branches per indexed repo store; `--prune` deletes stale ones for the current repo |
| `code repos` | — | List repositories with a code graph indexed |
| `code serve` | — | Run the code-graph MCP server standalone (`code_*` tools only) |
| `code setup` | `--dry-run` | Install the omnigraph binary for standalone `witan-code` use |

```bash
witan code index .
witan code deps --kind env_var
```
