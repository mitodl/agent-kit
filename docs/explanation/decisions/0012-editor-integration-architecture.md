<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan/docs/adr/0012-editor-integration-architecture.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan/docs/adr/0012-editor-integration-architecture.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/adr/0012-editor-integration-architecture.md).

# 12. Editor integration: one language server host, a launch contract, ACP, and thin clients

- Status: Accepted
- Date: 2026-11-17
- Deciders: witan platform owners
- Tracking: task `tk-spec-and-adr-editor-neutral-integration-architec-2d4ba2`,
  project `wp-witan-ide-integration-editor-neutral-council-and-914672`
- Related: `docs/adr/0003-atomic-task-claims-cas.md` (the claim a launch
  takes, and the fencing the release command adds to); `docs/adr/0005-secure-cli-path-into-deployed-witan.md`
  (the CLI's path into a deployment, which the language server reuses);
  `docs/adr/0011-witan-ui-read-transport.md` (the web UI's read-only binding,
  which this ADR does not widen); project
  `wp-witan-runners-harness-and-host-agnostic-fan-out--3fef69` (the harness
  adapter interface `witan acp` builds on); memory
  `pf-editor-capability-matrix-for-witan-ide-integrati-a7ca76` (the editor
  survey every claim about an editor below comes from)

## Context

Witan is reachable through MCP tools, the `witan` CLI and the web UI. None of
them sit where a person reads code, so someone working in an editor leaves it
to see what work is ready, who calls a symbol across repos, or which memories
concern the file they have open. This project brings the council and code
graphs into editors and lets a person start an agent on a task or project from
there.

The target is several editors, not one. VS Code goes first, but JetBrains IDEs,
Zed, Sublime Text, Neovim and Emacs have to stay reachable without a rewrite.
So the question this ADR answers is where each piece of logic lives, such that
adding an editor means writing a thin client and nothing else.

### What the editors can do

The survey (`pf-editor-capability-matrix-for-witan-ide-integrati-a7ca76`,
checked 2026-11-17 against editor and plugin source) settles the constraints.

1. Every target editor speaks LSP, through a built-in client (VS Code via the
   `vscode-languageclient` library, Zed, Neovim 0.12, Emacs eglot) or a plugin
   (LSP4IJ and the IntelliJ Platform LSP API, sublimelsp/LSP, lsp-mode). With
   no client code of ours, every one of them shows hover and pushed
   diagnostics.
2. CodeLens is not universal. eglot has none (`:codeLensProvider` appears only
   in its ignored-capabilities list), and Zed implements it but ships with
   `code_lens: "off"`.
3. The result of `workspace/executeCommand` reaches client code in VS Code
   (stock), Neovim (`Client:exec_cmd`), eglot (`eglot-execute`), lsp-mode,
   LSP4IJ and the IntelliJ API (from plugin code), and Sublime (from a helper
   package). Zed discards it and gives extensions no way to call a command.
4. Custom (non-standard) LSP methods need client code everywhere they work at
   all, and Zed cannot do them: its non-standard methods are hard-coded in Zed
   core per server.
5. An extension can open a terminal with a given argv, cwd and env in VS Code,
   JetBrains (2025.3+, experimental API), Neovim (`jobstart(..., {term=true})`)
   and Emacs (`make-term`). Sublime needs the Terminus package. A Zed extension
   cannot open a terminal at all; the nearest thing is a `tasks.json` task.
6. ACP is native in Zed and JetBrains AI Assistant, and both install agents
   from the ACP Registry (Zed deprecated ACP extensions in v1.5.0). Neovim
   (CodeCompanion, avante) and Emacs (acp.el with agent-shell) have plugins.
   VS Code has only third-party clients. JetBrains documents ACP as
   unsupported under WSL.
7. A tree view or webview exists in VS Code and JetBrains (JCEF), and a
   webview in Emacs only when built with xwidgets. Neovim, Sublime and Zed have
   none; Zed extensions have no UI surface at all.

The VS Code chat-panel spike
(`tk-spike-can-a-vs-code-extension-hand-a-prompt-to-c-eea4b3`, run live on VS
Code 1.146.0) adds one more fact. A VS Code extension can submit a prompt into
a new Copilot agent session with the internal command
`workbench.action.chat.openNewSessionSidebar.agent-host-copilotcli`, and can
only prefill the Claude Code extension's input
(`claude-vscode.editor.open(undefined, prompt)`). Neither path lets the caller
set the agent's environment.

### How a launch's claim reaches the agent

A claim's holder is the author qualified by a digest of the agent session id
(`_claim_holder`, `witan/server.py:897`). `witan task run` claims with no
session id (`witan/cli/run_helpers.py:273`), so from a plain terminal the claim
records the bare identity. The launched agent then renews with its own session
id, and `_own_claim` (`witan/server.py:989`) accepts that: a bare claim is its
caller's own to renew, and the renewal records the qualified holder. The prompt
already tells the agent this (`run_helpers.py:205-216`). So a launcher does not
need to know the agent's session id in advance, and nothing has to carry one
down to the agent.

The trap is an id the launcher picks up by accident. `_claim_holder` falls back
to `$CLAUDE_SESSION_ID`, `$PI_SESSION_ID` or `$CODEX_SESSION_ID`
(`witan/session_state.py`), and the CLI proxy injects the same value into tools
that take `session_id` (`witan_core/remote/proxy.py`, `_map_args`). An editor
started from a terminal inside an agent session inherits that agent's id, so
its language server would claim under a session that is not the one it
launches, and the launched agent's renewal would be refused as held by someone
else.

## Decision

Witan ships three editor-neutral layers, and each editor gets a client that
owns UI and nothing else.

### 1. The layering rule

| layer | ships in | owns |
|---|---|---|
| LSP host | witan-core, `lsp` extra | the protocol, document sync, workspace-folder to repo and target mapping, plugin discovery, merging plugin results, the command table |
| feature plugins | witan-council, witan-code | what to show for a document or position, and the commands each package contributes |
| launch contract | witan-council | deciding what to run (claim, prompt, argv, env) and returning it as data |
| `witan acp` | witan-council | an ACP agent that takes a launch from the contract and drives the real harness |
| editor client | `packages/` in agent-kit, one per editor | starting the server, binding its commands to editor UI, running a launch spec, rendering lists |

A client holds no witan logic. It never reads the graph except through the
server's commands, never handles a credential, and never decides what to run.
Anything a second editor would also need is a server or contract change, and a
feature that only works in one client is a defect against this project.

### 2. The language server

The host lives in witan-core behind a new `lsp` extra, built on pygls 2.x
(lsprotocol 2025.0.0, LSP 3.17). witan-core's base dependencies stay empty.
witan-council and witan-code each gain an `lsp` extra that pulls in
`witan-core[lsp]`, and ol-agent-kit depends on both with it, so the
meta-package installs a working server.

Features arrive as plugins discovered through an entry-point group,
`witan.lsp.plugins`, the pattern witan-council already uses for scanners
(`witan/scan/registry.py`) and screen classifiers
(`witan/screen/classifier.py`). witan-council and witan-code each register one.
`witan lsp` and `witan-code lsp` both start the same host, so an editor runs one
witan language server whichever packages are installed, and a person with only
witan-code gets the code features.

A plugin declares the capabilities it provides. One of them is symbol
resolution: mapping a document position to a witan-code symbol id. witan-code
provides it (from `code_symbols_in_file`); council's symbol-scoped features
(tasks and memories whose `symbol_refs` name the symbol) consume it through the
host and switch off when no plugin provides it. This replaces a "is witan-code
mounted" check.

The host merges what plugins return: one hover built from each plugin's
section, one CodeLens list in a fixed plugin order, one diagnostics set per
document.

#### Read path

Each plugin reads through its own package's existing dispatch: in-process
against the local store, or a `RemoteServerProxy` over `/mcp` when a deployment
is configured (ADR-0005). There is no second read path and no direct
`queries/*.gq` access. Reusing it inside a long-lived server takes three
changes, which the skeleton task owns.

- Target per workspace folder. Today `_srv()` (`witan/cli/_common.py:69`,
  `witan_code/cli.py:57`) caches one process-wide dispatcher, and `match_*`
  routing reads the process cwd or `CLAUDE_PROJECT_DIR`
  (`witan_core/target_config.py:42`). The host resolves the target for each
  workspace folder with that folder as the path. `--target` and `WITAN_TARGET`
  still win over matching, as on the CLI. A deployment target gets its own
  `RemoteServerProxy`, one per target. The local store cannot be split the
  same way: both servers build their configuration and graph client at import
  (`witan/server.py:190-210`, `witan_code/server.py:28`), so a process holds
  one local store. The host serves the first local store a folder resolves to,
  and a folder that resolves to a different local store gets no features and
  one Information diagnostic saying why, rather than reads and claims against
  the wrong store. The local path still goes through `local_server`'s
  pre-import guard (`witan/cli/local_dispatch.py`).
- Branch per folder. witan-code follows the process's own checkout when a read
  names no branch (`_resolve_branch`, `witan_code/server.py:182`), which is
  wrong for a second folder or a worktree. The code plugin passes `branch=`
  with each folder's checkout branch on every read, symbol resolution
  included. `code_find_references` takes no `branch` today and gains one.
- Dispatch off the event loop. The sync dispatch (`_fn`, and the proxy's
  `__getattr__`) runs its own `asyncio.run` per call, which cannot nest inside
  pygls's loop. The host runs plugin calls on one worker thread, where that is
  allowed, rather than rewriting the dispatch.
- Data, not output. Nothing a plugin calls may print to stdout, which is the
  JSON-RPC channel, or raise `SystemExit`. Logging goes to stderr, the rule
  `witan serve`'s stdio transport already follows, and the launch logic is
  refactored into a function that returns the spec (section 3).

A launch claim is always bare. The shared launch function (section 3) removes
every name in `SESSION_ID_ENV_VARS` (`witan/session_state.py:51`) from its
process's environment before it claims, so every launcher gets the same
behavior: the language server, `witan task run` with or without `--emit-spec`,
`witan acp`, and a Zed `tasks.json` task running `witan task run`. A launcher
is not an agent session, and an inherited id is the trap described in the
context. The same ids must not reach the launched agent either, which the
spec's `unset_env` handles (section 3). This also changes `witan task run`
started from inside an agent session, which today claims under the outer
session's id.

`has_terminal()` is false under piped stdio, so a tool that can elicit takes its
`default_when_unsupported` branch. The plugins pass every argument a tool would
otherwise ask about (the repo resolved from the workspace folder, above all), so
no feature depends on that default.

The host holds the tool binding as an enumerated list, as ADR-0011 does for the
UI: the tools named in the feature map below, plus `task_list`, `task_get`,
`workflow_project_get`, `task_ready`, `recall`, `task_claim` and `task_release`,
which the launch contract uses. A plugin that needs a tool outside the list
amends this ADR.

#### Feature map

| feature | plugin | LSP method | fallback where the method is missing |
|---|---|---|---|
| callers and cross-repo impact of a definition | code | `textDocument/codeLens`, resolved lazily by `codeLens/resolve` (`code_callers`, `code_cross_repo_impact`) | the same text in hover, and a code action that runs the command |
| providers and consumers of an env var, endpoint, package or service under the cursor | code | `textDocument/hover` (a new `code_interface_bindings_in_file`, then `code_interface_providers`, `code_interface_consumers`) | none needed |
| cross-repo definition and references | code | `textDocument/definition`, `textDocument/references` (`code_find_definition`, `code_find_references`) | a location the client cannot open (repo not checked out) comes back as hover text naming repo, path and line |
| the open branch has no index | code | one Information diagnostic on the first line (`code_indexed_branches`) | none needed |
| memories whose `symbol_refs` no longer resolve | council | `textDocument/publishDiagnostics`, Information severity, on the first line of the file the ref names, listing the memory and its `candidates` (`memory_stale_refs`) | a ref whose file no longer exists has no document to attach to, so `witan.listStaleRefs` returns every ref for the workspace repo |
| tasks and memories that reference a symbol | council, needs symbol resolution | hover, and CodeLens on the symbol (`recall` with `symbol_id`, whose `symbol` section lists the tasks) | hover only |
| ready work and active projects | council | `workspace/executeCommand` `witan.listReadyTasks`, `witan.listProjects` (`task_ready`, `task_for_branch`, `workflow_project_list`) | see section 5 |
| the person's own work in flight | council | `workspace/executeCommand` `witan.listMyWork` (`task_list` with `status="in_progress"`, `assignee="@me"` and the workspace repo; `task_ready` leaves out tasks with a live claim) | see section 5 |
| launch, and release after a failed launch | council | `workspace/executeCommand` `witan.runTask`, `witan.runProject`, `witan.releaseTask` | see section 5 |

No tool today maps a position to an interface binding: `code_interface_providers`
and `code_interface_consumers` take a `(kind, key)` that the hover has to find
first. `code_interface_bindings_in_file(repo, path)`, returning each binding's
kind, key and line, is new witan-code work that this ADR adds to the bound
list. `code_indexed_branches` returns which branches are indexed but no commit
or timestamp, so the server reports a branch with no index, not a stale one.

The server uses no custom LSP methods. Zed cannot receive them and Sublime
needs a helper package for each one, so everything goes through standard
methods and command results. Commands are namespaced `witan.*` and advertised
in `executeCommandProvider.commands`.

Teammate-written text (task descriptions, comments, memory content) reaches the
editor through hover and lens titles, and through a launch into an agent's
prompt. The server renders it as escaped plain text inside its markdown, so a
title cannot inject a link or an image; the only links in a hover are ones the
server builds (`ui_url`). Read-side screening follows whatever
`tk-screen-untrusted-content-returned-by-witan-tools-043ff5` settles.

### 3. The launch contract

LSP cannot open a terminal, so the server decides what to run and the client
runs it. The logic in `_run_task_slug` and `_run_project_slug`
(`witan/cli/run_helpers.py`) is refactored into a function that builds the
spec and returns it as data, with no console output and no `SystemExit`.
`witan task run` and `witan project run` call it and then either launch, as
today, or with `--emit-spec` print the spec as JSON. `witan.runTask` and
`witan.runProject` call the same function in-process and return the same
object; neither parses CLI output.

```json
{
  "version": 1,
  "kind": "task",
  "slug": "tk-...",
  "launchable": true,
  "argv": ["claude", "--model", "opus", "<prompt>"],
  "cwd": "/home/me/code/repo",
  "env": {"WITAN_TARGET": "prod"},
  "unset_env": ["CLAUDE_SESSION_ID", "PI_SESSION_ID", "CODEX_SESSION_ID", "CLAUDE_PROJECT_DIR", "WITAN_REPO"],
  "handoff": false,
  "prompt": "<prompt>",
  "agent": "claude",
  "model": "opus",
  "claim": {"claimed": true, "assignee": "me@example.edu", "claimed_at": "2026-11-17T16:20:04.607+00:00", "overlaps": []}
}
```

- The commands take the workspace folder they were invoked from. `cwd` is that
  folder when it maps to the task's repo, or when the task or project has no
  repo. Otherwise the server picks the open folder that maps to the task's repo
  (for a project, to any repo in its set), and refuses with
  `launchable: false` and `reason: "no_workspace"`, naming the repo, when no
  open folder does. This check runs before the claim, so a refusal never
  leaves a lease behind.
- The client starts from the environment it would give any terminal, removes
  every name in `unset_env`, then sets `env` over it. It never uses a
  strict-env mode. An editor started inside an agent session hands that
  session's variables to every terminal it opens, and they would reach the
  launched agent: an inherited `CLAUDE_SESSION_ID` outranks Pi's or Codex's own
  id in `current_session_id`, and an inherited `CLAUDE_PROJECT_DIR` or
  `WITAN_REPO` overrides the target matching and repo detection that `cwd`
  would otherwise drive (`witan_core/target_config.py:42`, `witan/repo.py`).
  So `unset_env` always lists every name in `SESSION_ID_ENV_VARS` plus
  `CLAUDE_PROJECT_DIR` and `WITAN_REPO`.
- `env` pins the target. It sets `WITAN_TARGET` to the target the folder
  resolved to, whether that came from `--target`, `WITAN_TARGET` or the
  folder's `match_*` rules. When the folder resolved to the local store, which
  has no target name, `unset_env` also lists `WITAN_TARGET`, and the agent's
  matching from `cwd` reaches the same local store. That matching runs without
  the overrides `unset_env` removes, so it can land on a named target the
  launcher never matched (an inherited `CLAUDE_PROJECT_DIR` steered the
  launcher's own matching elsewhere). The launcher checks this before
  claiming and refuses with `reason: "target_mismatch"`, naming the target in
  `target`, because the local store cannot be pinned. Either way the agent
  talks to the graph the claim was taken in. `env` never contains a credential; the
  launched agent's own witan resolves the token from the `witan login` store,
  as any agent does today.
- A task launch claims first, as `_run_task_slug` does, and the claim is bare
  (section 2). The launcher drops `task_claim`'s `warning` key from the claim
  it returns, because the warning advises passing a `session_id`, and a bare
  launch claim is intended. Any refusal returns `launchable: false` with
  `task_claim`'s result passed through as `claim` (its `reason`, plus
  `held_by` and `remedy` where the refusal has them) and no `argv`. A missing
  task is `reason: "not_found"`. Under `--emit-spec` every refusal is also a
  non-zero exit.
- A project launch claims nothing and has `claim: null`. `_project_run_prompt`
  hands over up to 20 ready tasks and the agent claims each before starting it;
  claiming up front was rejected because it holds leases on work nobody starts.
- `version` is the contract version. Adding a field keeps it; removing or
  changing one bumps it. A client refuses a spec whose version it does not know
  and says so, rather than guessing.

#### Release after a failed launch

`witan.releaseTask` exists for a launch that failed after claiming (the agent
was not on PATH, the terminal never opened), so the task does not sit held for
the 60-minute lease. `task_release` as it stands is too wide for that: it
matches holders at the person level (`_same_person`, `witan/server.py:976`),
and it returns the task to `open` whatever its status, so a late click could
release a claim the launched agent had already renewed, or reopen a task that
was closed after the launch.

So `task_release` gains a fence, `if_claimed_at`. When it is given, the release
happens only if the task is `in_progress` and its `claimed_at` is still the
same instant, checked on the row the write is fenced on, as ADR-0003's claims
are. The comparison parses both sides with `witan_core.timeutil.parse_timestamp`
rather than comparing strings: `task_claim` returns `now_iso()`, which carries
a `+00:00` offset, and the graph returns its `DateTime` columns without one, so
the two strings never match.
`witan.releaseTask` passes the spec's `claim.claimed_at`. Once the agent renews,
`claimed_at` moves and the release is refused; a closed task is refused too. It
never passes `force`.

#### Why the spec carries no session id

An earlier draft minted a session id per launch and handed it down as
`WITAN_SESSION_ID`, placed first in `SESSION_ID_ENV_VARS`. That is rejected.
Every process started in the launched terminal would inherit the id, so a
second agent or a nested `witan task run` there would share one claim holder,
which is the silent double-claim `_claim_holder` exists to prevent. The
workflow-session handle is keyed by the same id (`witan/session_state.py`), so
one agent's Stop hook could end the other's session. The bare claim above
needs no id at all.

The cost is that a client cannot tell which tasks a launched project session
picked up. Until something needs that, a client shows the person's own
`in_progress` tasks instead, from `witan.listMyWork`. `WITAN_SESSION_ID` as the runners project uses it
(one id per worker slot, set by the runner) is a separate decision, and nothing
here depends on it.

#### Where a launch runs

In the open workspace folder chosen above. The server does not create
worktrees. Whether an agent works in a worktree is the agent's convention
(several of ours call `EnterWorktree` themselves), and a server-created worktree
would need every client to open a new folder, which Neovim, Sublime and Zed
handle differently or not at all. A `worktree` option can be added to the
contract later without a version bump.

### 4. Writes

The language server and the editor clients may claim (inside a task launch),
launch, and release (fenced, as above). Closing and commenting stay with the
agent and the CLI.

This is not a change to ADR-0011. That ADR binds the _web UI_ to reads because
it is a browser surface with its own token and origin story. The language
server is a local process acting as the person, through the same dispatch and
token as `witan task run`, which already claims. The webview that will host the
web UI's views inside VS Code is the web UI and stays under ADR-0011.

### 5. Launch modes and per-editor fallbacks

Launch modes, in order of delivery:

1. Terminal, from the launch spec. VS Code, JetBrains, Neovim and Emacs run the
   spec's `argv`, `cwd` and `env` in their own terminal.
2. ACP, through `witan acp`. It is a client of the launch contract like an
   editor is, wraps the real harness through the runners project's adapter
   interface, and forwards session updates. It ships as one ACP Registry entry,
   which covers Zed and JetBrains; Neovim and Emacs reach it through their ACP
   plugins.
3. Chat-panel handoff, VS Code only and optional. It hands a spec's `prompt` to
   the agent's own panel: Copilot's `openNewSessionSidebar.agent-host-copilotcli`
   submits it, and the Claude Code extension's `claude-vscode.editor.open` only
   prefills it. A handoff never claims. It uses a task spec built with the
   claim off (the existing `claim=False` path of `_run_task_slug`, whose prompt
   tells the agent to claim first) or a project spec, because a prefilled
   prompt the person never submits would otherwise hold a lease for an hour
   with no agent behind it and no failure the client could release on.
   Neither command carries env or `unset_env`, so the panel agent resolves its
   target and repo from VS Code's own environment. The server sets the spec's
   `handoff` to true only when that resolution matches the launch's: its own
   environment (which it shares with the VS Code process that started it) has
   none of `WITAN_TARGET`, `CLAUDE_PROJECT_DIR` or `WITAN_REPO`, it was not
   started with `--target`, and the window has one workspace folder. The client
   offers a handoff only for a spec with `handoff: true`. Both commands are internal to their owners, so the VS Code client
   checks for them at runtime and hides the action when either is absent.

What an editor without a feature does instead:

| missing | editors | instead |
|---|---|---|
| CodeLens | eglot; Zed by default | the lens content is in hover, and its command is a code action |
| `executeCommand` result | Zed | launch through ACP (`witan acp`), or a `tasks.json` task running `witan task run <slug>`; list ready work through witan's MCP tools in Zed's agent |
| terminal API | Sublime without Terminus, Zed | as above for Zed; Sublime requires Terminus, and without it the client shows the argv to run |
| tree view or webview | Neovim, Sublime, Emacs without xwidgets, Zed | the list commands return JSON the client renders natively (quickfix list, quick panel, `tabulated-list-mode`); the board is the web UI at `ui_url` in a browser |

### 6. Clients and release

Editor clients live under `packages/` in agent-kit and publish under the `mitol`
publisher on both the VS Code Marketplace and Open VSX. The neutral interface is
validated in Neovim and Emacs (eglot and lsp-mode) before the VS Code client
grows features of its own, so that eglot exercises the CodeLens fallback.

## Alternatives considered

The language server in witan-council, mounting code features when witan-code
is installed (as `mount_witan_code` does for the MCP tools). Rejected because
witan-code runs on its own with its own CLI and login, and a person with only
witan-code would get nothing in the editor.

Two servers, one per package. Rejected because an editor would start two witan
processes on the same files, both resolving targets and tokens, and a symbol's
hover would arrive in two pieces with no shared ordering.

Delivering the launch spec as a custom server-to-client request. Rejected by
the survey: Zed cannot receive one and Sublime needs a helper package per
method, while a command result reaches client code everywhere except Zed, which
gets ACP instead.

A per-launch `WITAN_SESSION_ID`. Rejected; see "Why the spec carries no session
id".

The server spawning the agent itself. Rejected because the agent would run
without a terminal the person can see or type into, and as a child of the
editor's language-server process, which every editor restarts at will.

## Consequences

- witan-core gains a runtime role, hosting a long-lived server, in addition to
  being a library. The `lsp` extra keeps that out of every other install. A
  change that adds a host symbol and a plugin that calls it raises that
  server's `witan-core>=X` floor in the same change, and `just
  check-core-floor` catches a missed one.
- New work outside the language server itself: target resolution
  parameterised by a path instead of the process cwd (witan-core),
  `task_release`'s `if_claimed_at` fence (witan-council),
  `code_interface_bindings_in_file` and a `branch` parameter on
  `code_find_references` (witan-code).
- One language server process serves one local store. A person whose open
  folders route to two different local stores gets features in the first and a
  diagnostic in the second. Lifting that needs the servers' import-time
  configuration made per-store, which is out of scope here.
- A bare launch claim can be renewed by any of the person's sessions until the
  launched agent renews it. `_own_claim` already accepts that for
  `witan task run`, and the agent's first renewal closes it.
- The internal VS Code commands behind chat handoff can change in any VS Code
  release. Mode 3 stays optional, and the VS Code client's CI pins and tests the
  command names per release.
- Zed users get hover, diagnostics, definition and references from the
  language server, and launch only through ACP or a `tasks.json` task. Using a
  language server for task coordination may also meet Zed's extension rule
  against misusing the API to work around its limitations; a Zed extension, if
  one is written, registers the language server only.
- The implementation tasks in this project are checked against this ADR and
  amended where they disagree.
