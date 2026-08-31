# witan User Guide

witan is a team-wide shared knowledge and coordination graph for coding
agents. It solves two related problems: (1) engineering knowledge —
patterns, project facts, lessons — discovered by one agent session is
normally lost the moment that session ends, and (2) multiple agents (or
people) working on the same repo have no shared view of what work exists,
what's claimed, and what's blocked. witan stores both in one graph, backed
by [Omnigraph](https://github.com/ModernRelay/omnigraph), and exposes it over
MCP so any agent platform (Claude Code, Pi, GitHub Copilot, OpenCode) reads
and writes the same store without platform-specific glue.

Distributed on PyPI as `witan-council` (the `witan` project name was already
taken); the import path, console command, and every tool/CLI name are still
`witan`. Only the install artifact's name changed.

## Feature set

- **Memory search & store** — full-text (BM25) search over patterns, project
  facts, lessons, and agent context, with graph-aware re-ranking. See
  [Day-to-day loop](#day-to-day-loop) below and the `memory` command in the
  [CLI Reference](CLI_REFERENCE.md#memory).
- **Workflow project tracking** — track an engineering objective across
  multiple agent sessions (discovery → spec → implementation → delivery
  phases), with session hand-off state. See
  [`witan-project-tracker`](../witan/skills/witan-project-tracker/SKILL.md)
  and the `project`/`projects` commands in the
  [CLI Reference](CLI_REFERENCE.md#projects).
- **Task tracking with dependencies** — a hierarchical, dependency-aware task
  tracker (epics → sub-issues, `blocked_by`, advisory claims with lease
  expiry) shared across agents/users. See
  [`witan-task`](../witan/skills/witan-task/SKILL.md) and the
  [CLI Reference](CLI_REFERENCE.md#tasks).
- **Code-branch tracking** — links a git branch to the task/project it
  carries, wired in automatically by `task_claim` and
  `workflow_session_start`. See [Code branch tracking](#code-branch-tracking).
- **Write-path secret/PII scanning** — every memory/task/project/session
  write is scanned for secrets and PII before it's persisted, with
  block/redact/warn enforcement and a plugin mechanism. See
  [`docs/write-path-scanning.md`](write-path-scanning.md) and the
  [`scan` command](CLI_REFERENCE.md#scan).
- **Code graph integration (`witan-code`)** — when the sibling `witan-code`
  package is installed, `witan code …` mounts tree-sitter-derived symbol
  search, references, and cross-repo impact analysis into the same CLI/MCP
  server. See the [CLI Reference](CLI_REFERENCE.md#code-witan-code-only) and
  [`../witan-code/README.md`](../../witan-code/README.md).

## Installation

Two install shapes, depending on whether your agent platform needs the
`witan` binary on `PATH`:

- **Persistent CLI** — required for **Claude Code** and **Pi** (their
  hooks/extensions shell out to `witan` directly):

  ```bash
  uv tool install witan-council
  witan setup --agent claude   # or: pi | all
  ```

  To track pre-release/unreleased code instead of the latest PyPI release,
  install from the git repo directly:

  ```bash
  uv tool install git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan
  witan setup --agent claude   # or: pi | all
  ```

- **MCP-only** — sufficient for **Copilot**, **OpenCode**, and **Kilo**,
  whose MCP server launches via `uvx` on demand, so nothing needs to stay
  installed:

  ```bash
  uvx --from witan-council witan setup --agent copilot   # or: opencode | kilo
  ```

  Same pre-release option here — swap in the `git+…` source:

  ```bash
  uvx --from git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan \
      witan setup --agent copilot   # or: opencode | kilo
  ```

See the main [README](../README.md#quick-start) for the manual-wiring
fallback (`./install.sh` + hand-editing agent MCP config).

## First-run setup

`witan setup --agent <agent>` does four things, in order:

1. Downloads the pinned `omnigraph` binary release to `~/.local/bin/omnigraph`
   (skipped if already present).
2. Writes a starter `~/.config/witan/config.toml` — every optional setting
   commented out at its actual default — unless one already exists.
3. Copies bundled skills and hooks/extensions into the target agent's config
   directories (e.g. `~/.claude/skills/`, `~/.claude/hooks/`).
4. Merges the witan MCP server entry into that agent's config file.

Re-run it after every witan upgrade to refresh installed files and pick up an
`omnigraph` version bump. Pass `--dry-run` to preview without writing
anything, and `--author "Your Name"` to set graph attribution up front
(otherwise it falls back to `git config user.name`, then `$USER`).

The graph itself lives at `~/.local/share/witan/graph.omni` by default (a
local Omnigraph store) — nothing else to provision for local-disk mode. See
[Operating modes](#operating-modes) for RustFS and remote-server setups.

## Day-to-day loop

A typical session:

1. **Check ready work.**

   ```bash
   witan tasks --ready
   ```

   Lists open tasks with no open blockers, ordered by priority, scoped to
   the repo you're standing in (detected from `origin` in `.git/config`).
   Add `--project wp-<slug>` to scope to one workflow project, or
   `--all-repos` to see everything.

2. **Claim one and start working.**

   ```bash
   witan run tk-fix-flaky-retry-abc123
   ```

   Sets the task `in_progress` under your author name (an advisory lease,
   not a hard lock — see `task_claim` in the
   [CLI Reference](CLI_REFERENCE.md#run)), then launches your configured
   agent CLI with a prompt seeded from the task's title/description/symbol
   refs. Pass `--dry-run` to print that prompt without claiming or
   launching, or `--claim=false` to launch without claiming.

   If you're inside an already-running agent session (rather than the
   `witan run` launcher), use the MCP tools directly: `task_claim`, and
   `task_close` with a `resolution` when you're done. Filing follow-up work
   discovered mid-task: `task_create(discovered_from=["tk-…"], …)`.

   `task_get` returns the task's **comments** along with its description —
   read them before executing, because a comment is how someone tells you
   the description is wrong without overwriting it.

1. **Correct someone else's task instead of clobbering it.**

   ```bash
   witan task comment tk-… "That guard cannot fire on these pipelines — …"
   ```

   Use this (`task_comment` from an agent session) when you find a problem
   with work you are *not* executing. It is attributed, timestamped, and
   append-only; it changes nothing about the task, so it neither destroys
   the other author's text (`task_update` would) nor adds an item that is
   not work to everyone's ready list (`task_create` would). Unread comments
   on a task you hold are pushed into your session context.

3. **Store what you learned.**

   Any durable, shareable fact — a coding pattern, a project-specific quirk,
   a lesson from a bug you just fixed — belongs in witan, not your agent's
   private session memory, so other sessions and teammates can find it:

   ```
   memory_store(kind="pattern", title="...", content="...", repo=<auto>, tags=[...])
   ```

   Search before you start new work so you don't rediscover something
   already recorded:

   ```bash
   witan memory "flaky retry" --kind pattern
   ```

4. **Track a multi-session project.** For work that spans more than one
   session, create (or resume) a `WorkflowProject` rather than tracking
   state in your head:

   ```bash
   witan project create "Migrate auth to OAuth2" --phase discovery
   ```

   Then `workflow_session_start` at the top of each session (the
   [`witan-workflow`](../witan/skills/witan-workflow/SKILL.md) skill
   automates the picker), and `workflow_session_end` with a summary before
   you stop — this is what lets a *different* session or agent pick the
   thread back up later. `workflow_project_advance` moves the project to
   its next phase; `workflow_project_complete` closes it out and assembles a
   `WorkflowTrace` corpus record from every session for later pattern
   mining.

### Code-branch tracking

No dedicated command — this rides along automatically, best-effort:
`workflow_session_start` and `task_claim` both upsert a `CodeBranch` node
linking the current checkout's repo+branch to the project/task in flight, so
"which branch carries task X" is a one-hop graph query. It silently no-ops
outside a git repo, on a detached HEAD, or against a store that predates the
feature — never a hard requirement for the tool call it's attached to.

## Operating modes

Three ways to point witan at a store, in increasing order of shared-ness:

- **Local disk (default)** — no extra infrastructure; the graph lives at
  `~/.local/share/witan/graph.omni`. What you get out of the box.
- **Local RustFS** — an S3-compatible store running in Docker on your
  machine, for exercising the remote-server code path without standing up
  real infrastructure: `RUSTFS=1 ./install.sh`.
- **Deployed service (shared, multi-user)** — the team mode. Your CLI and
  agent become MCP clients of a deployed endpoint, authenticated with your own
  Keycloak identity, with per-actor Cedar authorization over one shared graph.
  Configured with a `[targets.*]` block plus `witan login`.

To join a deployed service, follow
[**Pointing your CLI and agent at the deployed witan**](deployed-witan-onboarding.md),
and migrate the history you already have with the
[migration runbook](migration-runbook.md#local-shared-the-cutover). Sequence
the two together — a store you keep writing to after its export was taken has
a tail nobody will merge.

Note that pointing `WITAN_MEMORY_URI` straight at an omnigraph-server is a
*different*, lower-level mode: it addresses the data tier directly with a
shared bearer token and no per-user identity. That is how a self-hosted or
in-cluster maintenance process connects, not how a person does. See
[`docs/internals/agent-memory.md` § Operating Modes](../../../../docs/internals/agent-memory.md#6-operating-modes)
for the graph schema and server-deployment mechanics.

`config.toml` can also define named `[targets.*]` sections that route
different repos/orgs at different stores (e.g. work vs. personal) — see the
`load()` docstring in `witan/config.py` and the commented example block that
`witan setup` writes into your starter config file.

## Troubleshooting

- **`witan: command not found` in a hook.** Claude Code/Pi hooks and
  extensions call the `witan` binary directly — it must be on `PATH` for the
  user those hooks run as. `witan setup` warns explicitly if it can't find
  `witan` on `PATH` when you run it; install with `uv tool install
  witan-council` (or, for pre-release code, `uv tool install
  git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan`).
- **`omnigraph` binary missing.** `witan setup` downloads it to
  `~/.local/bin/omnigraph`; if that directory isn't on `PATH`, both the CLI
  and MCP server will fail to reach the graph. Re-run `witan setup` after an
  `omnigraph` version bump (tracked via the `omnigraph-version` Renovate
  customManager) to refresh the pinned binary.
- **`witan migrate storage` needed after an omnigraph upgrade.** omnigraph
  uses strict single-version on-disk storage — a release that bumps the
  internal schema refuses to open a store an older binary wrote. If your
  local graph suddenly won't open, run `witan migrate storage`; it detects
  the old binary, exports with it, and reloads into the new format,
  preserving nodes/edges/vectors (commit history and branches are not
  preserved; the original is kept as `<store>.pre-migrate`).
- **No tasks/projects showing up for this repo.** Repo scoping is detected
  from the `origin` remote in `.git/config` (or `WITAN_REPO` if set). Work
  created without repo context, or from a different remote URL form (SSH vs.
  HTTPS — both normalize to the same canonical URI, but a mismatch elsewhere
  won't), won't show up under `--repo`; pass `--all-repos` to check.
- **Code-branch tracking silently absent.** `workflow_session_start` and
  `task_claim` only upsert a `CodeBranch` when they can detect a git repo and
  a named branch. Detached HEAD, a directory outside any git repo, or a
  store that predates the `CodeBranch` schema (run `witan migrate schema`)
  all cause a silent no-op — this is metadata riding along, not a
  requirement for the underlying task/workflow call to succeed.
- **A write got blocked or redacted unexpectedly.** That's the write-path
  content scanner (secrets block by default, PII redacts by default). Run
  `witan scan test "<the text>"` to see exactly which detector fired and why,
  and `witan scan rules` to see what's active. See
  [`docs/write-path-scanning.md`](write-path-scanning.md) for the full
  config surface if you need to tune or disable it.
- **`witan run`/`task run`/`project run` can't find your agent CLI.** It
  shells out to whatever `--agent` (or `WITAN_AGENT`/config default)
  resolves to, verbatim, on `PATH`; a `FileNotFoundError` prints
  `Agent '<name>' not found on PATH.` — install or alias that CLI, or pass
  `--agent` explicitly.
