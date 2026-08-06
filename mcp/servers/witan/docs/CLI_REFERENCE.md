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
| `login` | Authenticate to a deployed witan service (see [Remote mode](#remote-mode)) |
| `logout` | Forget the cached token for the deployed service (see [Remote mode](#remote-mode)) |
| `whoami` | Show the identity presented to the deployed service (see [Remote mode](#remote-mode)) |
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
| `--transport` | `stdio\|http\|streamable-http` | `stdio` | `WITAN_MCP_TRANSPORT` | MCP transport. `stdio` for local (Claude Desktop, `uvx`); the others bind a network listener. The legacy HTTP+SSE transport is not offered — MCP 2026-07-28 deprecates it |
| `--host` | str | `127.0.0.1` | `WITAN_MCP_HOST` | Interface to bind for HTTP transports (`0.0.0.0` inside a container) |
| `--port` | int | `8000` | `WITAN_MCP_PORT` | Port to bind for HTTP transports |
| `--path` | str | `/mcp` | `WITAN_MCP_PATH` | URL path the MCP endpoint is served on (HTTP transports only) |

```bash
witan serve --transport streamable-http --host 0.0.0.0 --port 8080
```

## Remote mode

By default every `witan` command runs **in-process** against the local graph
(`WITAN_GRAPH_URI`). To instead run commands against a *deployed* witan service
as your own Keycloak-authenticated user — inheriting the same per-user Cedar
scoping and audit trail as the agent traffic — set `WITAN_REMOTE_URL` and log
in. See [ADR-0005](adr/0005-secure-cli-path-into-deployed-witan.md).

For joining an actual deployment, prefer a named `[targets.*]` block over these
env vars (it scopes the deployment to the repos it covers, and routes
`witan-code` with it) — see
[**Pointing your CLI and agent at the deployed witan**](deployed-witan-onboarding.md).
A configured-but-unreachable remote **hard-fails**; it never falls back to your
local store, since a silent fallback would split the corpus in two.

| Env var | Required | Description |
|---|---|---|
| `WITAN_REMOTE_URL` | yes (to enable) | The deployed MCP endpoint, e.g. `https://witan.example.org/mcp` |
| `WITAN_OIDC_ISSUER` | yes | Keycloak realm issuer URL (where `witan login` discovers the device + token endpoints) |
| `WITAN_OIDC_CLIENT_ID` | no (`witan-cli`) | Public OIDC client id with the device grant enabled |
| `WITAN_OIDC_AUDIENCE` | no | Audience/resource to request, matching the deployment's expected `aud` claim |
| `WITAN_TOKEN_CACHE` | no (`~/.config/witan/tokens.json`) | Override the token cache path |

With those set, `_srv()` transparently dispatches every command over MCP —
`witan tasks`, `witan memory search`, `witan project show`, etc. all work
unchanged. Admin/migration commands (`witan apply-schema`, `witan migrate …`,
`merge-store`) are **not** available remotely — they have no per-user identity
and run in-cluster as `svc-witan-admin` (ADR-0005 path b).

Two arguments the deployed server cannot resolve for itself are filled in
client-side before dispatch: `repo` (it has no git checkout) and `session_slug`
(the protocol carries no session state, and a replica shares no filesystem with
your agent). The latter comes from the handle `witan session start` parked under
`$CLAUDE_SESSION_ID`, so memories written remotely keep their `SessionProduced`
provenance. Pass either explicitly to override.

```bash
export WITAN_REMOTE_URL=https://witan.example.org/mcp
export WITAN_OIDC_ISSUER=https://sso.example.org/realms/ol-platform-engineering
witan login          # opens a browser device-code flow, caches the token
witan whoami         # shows user / sub / derived actor id / token expiry
witan tasks --ready  # now runs against the deployment as you
witan logout         # forget the cached token
```

### `login`

Runs the OIDC device authorization grant (RFC 8628): prints a verification URL
and user code, waits for browser approval, then caches the access/refresh
tokens (mode `0600`). Refreshes happen automatically on later commands.

### `whoami`

Decodes the cached token and prints the endpoint, `preferred_username`,
`email`, `sub`, the derived `act-<id>` the server will scope you to, and the
token expiry. Refreshes the token first if it has expired.

### `logout`

Drops the cached token for the configured deployment only (tokens for other
deployments in the same cache are untouched).

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

### `migrate merge SOURCE`

Merge another store's data into this store, newest-record-wins on slug
collisions. For every node present in both (matched on type + slug) it keeps
whichever has the newer timestamp, rather than `omnigraph load --mode merge`'s
raw last-loaded-wins overwrite, which ignores content entirely. Rows only in
the source are added; rows only in the target are untouched; edge rows have no
slug and pass through unreconciled. Repeatable — re-running against an
already-merged target loads nothing.

| Flag | Type | Default | Description |
|---|---|---|---|
| `SOURCE` (positional) | str | required | A store URI (local path, `s3://`, `file://`, or an `http(s)://` omnigraph-server) **or** the path to an `omnigraph export` JSONL — anything ending `.jsonl` is read as an export rather than re-exported |
| `--target` | str \| None | configured store | Store URI to merge into. A missing local target is created and schema-applied; a missing remote one is assumed to exist |
| `--dry-run` | bool | `False` | Print the per-slug decision (`added`/`updated`/`kept-target`) without writing |

A `.jsonl` source is how a store crosses machines: Lance embeds absolute paths,
so a `.omni` directory cannot be copied, tarred, or streamed into a pod — only
its export can. A deployed graph is addressed as a server, not a store
(`http(s)://<host>:<port>/graphs/<graph-id>`), since omnigraph 0.8.1 rejects an
http(s) `--store`.

```bash
witan migrate merge ~/.local/share/witan-laptop-b/graph.omni --dry-run
witan migrate merge old-machine.omni --target new-machine.omni
witan migrate merge alice-export.jsonl --target http://127.0.0.1:8080/graphs/council
```

Moving a local store onto the deployed service is a distinct procedure — the
data tier is ClusterIP-only, so the merge runs in-cluster from a handed-over
export. See [the migration runbook](migration-runbook.md#local--shared-the-cutover)
and [ADR-0007](adr/0007-local-to-shared-store-migration-transport.md).

### `migrate topics`

Backfill `Topic` nodes from existing memory tags: for every distinct memory
tag, upsert a `Topic{kind:"topic"}` and a `Tagged` edge. Safe to re-run
(already-created topics/edges are skipped). Fails fast if the Topic schema
isn't applied yet (`migrate schema` first). No flags.

```bash
witan migrate topics
```

### `migrate dedupe-sessions`

Reconcile `WorkflowSession` nodes that a pre-upsert `workflow_session_start`
duplicated. Every call used to mint a node, so a hook retry, a transport
reconnect, or a deliberate re-call (once the only way to widen a project's repo
set) left extra sessions sharing one `session_id` — and
`workflow_project_complete` counts every linked session into its trace.

Sharing a `session_id` is not on its own evidence of duplication: one
`$CLAUDE_SESSION_ID` routinely spans several working stints, each closed with
its own summary. So only sessions that **overlap in time** are considered, and
within an overlapping run only the members with no summary of their own are
marked `superseded_by` the survivor. A marked session keeps its row and its
edges; it is simply skipped by every aggregate read. Runs where every member
wrote a real summary are printed for review rather than guessed at — resolve
those with `--supersede`.

Dry by default. Idempotent. Deliberately **not** part of `migrate all`: unlike
the other migrations this one makes a judgment call about corpus content.

Needs `migrate schema` first — the mark is stored in a `superseded_by` field
added alongside this command, and every session read now selects it, so an
existing store must be reconciled before it will serve them.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--apply` | bool | `False` | Write the marks instead of only reporting them |
| `--supersede` | list | — | `<duplicate-slug>=<survivor-slug>` pairs to mark regardless of the automatic rule; repeatable |

```bash
witan migrate dedupe-sessions                       # report only
witan migrate dedupe-sessions --apply
witan migrate dedupe-sessions --apply --supersede ws-dup-abc123=ws-real-def456
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

Auto-close the active WorkflowSession on agent stop: reads back the session
handle `workflow_session_start` returned (persisted client-side under
`$CLAUDE_SESSION_ID`) and passes its `session_slug` to `workflow_session_end`.
The call is dispatched the same way every other CLI command is, so it reaches
the deployment when `WITAN_REMOTE_URL` is set. No-op if there is no handle —
the session was already closed explicitly (Stop hook). No flags.

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
