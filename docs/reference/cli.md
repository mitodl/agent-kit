<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: the cyclopts command tree (`witan.cli.app`)
-->
# witan

```console
witan COMMAND [OPTIONS]
```

witan — agent memory, planning, and collaboration graph.

## Table of Contents

- [`login`](#witan-login)
- [`logout`](#witan-logout)
- [`whoami`](#witan-whoami)
- [`graph`](#witan-graph)
- [`inject-context`](#witan-inject-context)
- [`session-checkpoint`](#witan-session-checkpoint)
- [`optimize`](#witan-optimize)
- [`cleanup`](#witan-cleanup)
- [`memory`](#witan-memory)
- [`projects`](#witan-projects)
- [`project`](#witan-project)
    - [`status`](#witan-project-status)
    - [`tasks`](#witan-project-tasks)
    - [`create`](#witan-project-create)
    - [`update`](#witan-project-update)
    - [`advance`](#witan-project-advance)
    - [`complete`](#witan-project-complete)
    - [`block`](#witan-project-block)
    - [`unblock`](#witan-project-unblock)
    - [`run`](#witan-project-run)
- [`scan`](#witan-scan)
    - [`test`](#witan-scan-test)
    - [`rules`](#witan-scan-rules)
- [`session`](#witan-session)
    - [`start`](#witan-session-start)
    - [`end`](#witan-session-end)
    - [`sweep`](#witan-session-sweep)
    - [`list`](#witan-session-list)
- [`setup`](#witan-setup)
- [`target`](#witan-target)
    - [`add`](#witan-target-add)
    - [`list`](#witan-target-list)
    - [`remove`](#witan-target-remove)
- [`tasks`](#witan-tasks)
- [`task`](#witan-task)
    - [`create`](#witan-task-create)
    - [`close`](#witan-task-close)
    - [`claim`](#witan-task-claim)
    - [`release`](#witan-task-release)
    - [`update`](#witan-task-update)
    - [`link`](#witan-task-link)
    - [`unlink`](#witan-task-unlink)
    - [`run`](#witan-task-run)
- [`traces`](#witan-traces)
- [`trace`](#witan-trace)
    - [`list`](#witan-trace-list)
- [`migrate`](#witan-migrate)
    - [`schema`](#witan-migrate-schema)
    - [`storage`](#witan-migrate-storage)
    - [`merge`](#witan-migrate-merge)
    - [`topics`](#witan-migrate-topics)
    - [`repo-keys`](#witan-migrate-repo-keys)
    - [`dedupe-sessions`](#witan-migrate-dedupe-sessions)
    - [`all`](#witan-migrate-all)
    - [`claim-authorship`](#witan-migrate-claim-authorship)
- [`code`](#witan-code)
    - [`index`](#witan-code-index)
    - [`reindex`](#witan-code-reindex)
    - [`doctor`](#witan-code-doctor)
    - [`deps`](#witan-code-deps)
    - [`symbols`](#witan-code-symbols)
    - [`stitch`](#witan-code-stitch)
    - [`inject-context`](#witan-code-inject-context)
    - [`serve`](#witan-code-serve)
    - [`optimize`](#witan-code-optimize)
    - [`cleanup`](#witan-code-cleanup)
    - [`reap-views`](#witan-code-reap-views)
    - [`checkpoint`](#witan-code-checkpoint)
    - [`session-init`](#witan-code-session-init)
    - [`reindex-hook`](#witan-code-reindex-hook)
    - [`setup`](#witan-code-setup)
    - [`branches`](#witan-code-branches)
    - [`repos`](#witan-code-repos)
    - [`login`](#witan-code-login)
    - [`logout`](#witan-code-logout)
    - [`whoami`](#witan-code-whoami)
- [`serve`](#witan-serve)
- [`run`](#witan-run)

**Commands**:

* [`cleanup`](#witan-cleanup): Remove old Lance versions to reclaim disk (**destructive**).
* [`code`](#witan-code): witan-code — tree-sitter code graph + cross-repo bridge.
* [`graph`](#witan-graph): Visualize the workflow project and task dependency graph.
* [`inject-context`](#witan-inject-context): Print workflow context for the UserPromptSubmit hook.
* [`login`](#witan-login): Authenticate to the deployed witan service via the OIDC device grant.
* [`logout`](#witan-logout): Forget the cached token for the configured deployment.
* [`memory`](#witan-memory): Search memory (BM25), or with no query list memories (filtered by --kind).
* [`migrate`](#witan-migrate): One-shot, idempotent schema and data migrations.
* [`optimize`](#witan-optimize): Compact the graph store's Lance fragments (non-destructive).
* [`project`](#witan-project): Manage workflow projects.
* [`projects`](#witan-projects): List workflow projects (default: active in the current repo).
* [`run`](#witan-run): Claim a task and launch an agent to execute it.
* [`scan`](#witan-scan): Introspect and dry-run write-path content scanning (ADR 0001).
* [`serve`](#witan-serve): Run the witan MCP server.
* [`session`](#witan-session): Manage workflow sessions.
* [`session-checkpoint`](#witan-session-checkpoint): Auto-close the active WorkflowSession on agent stop (Stop hook).
* [`setup`](#witan-setup): Install witan for one or all supported coding agents.
* [`target`](#witan-target): Register and inspect named [targets.*] blocks (deployed witan endpoints).
* [`task`](#witan-task): Manage tasks.
* [`tasks`](#witan-tasks): List tasks for the current repo (or filtered).
* [`trace`](#witan-trace): Inspect corpus trace records.
* [`traces`](#witan-traces): List corpus workflow traces (default: current repo).
* [`whoami`](#witan-whoami): Show the identity the CLI presents to the deployed witan service.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*

## witan login

```console
witan login [OPTIONS]
```

Authenticate to the deployed witan service via the OIDC device grant.

Prints a verification URL and a user code; approve it in a browser, and the
resulting token is cached (mode 0600) and refreshed automatically for
subsequent ``witan …`` commands.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `--target`: a target with no ``match_*`` criteria, which never selects itself. Also
    settable via ``WITAN_TARGET``.

## witan logout

```console
witan logout [OPTIONS]
```

Forget the cached token for the configured deployment.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `--target`:

## witan whoami

```console
witan whoami [OPTIONS]
```

Show the identity the CLI presents to the deployed witan service.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `--target`:

## witan graph

```console
witan graph [OPTIONS]
```

Visualize the workflow project and task dependency graph.

Prints a Rich summary of projects and tasks, then optionally writes an
interactive HTML graph (vis-network) or a Graphviz DOT file.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `--repo`: Scope to a specific repo URI (default: current git repo).
* `--all-repos, --no-all-repos`: Include projects and tasks from every repo. *[default: False]*
* `--status`: Project status filter: active | completed | abandoned.
    Defaults to ``active``. Pass an empty string to include all. *[default: active]*
* `--all-tasks, --no-all-tasks`: Include closed tasks (default: open + in_progress + blocked only). *[default: False]*
* `--no-belongs-to, --no-no-belongs-to`: Omit dashed task→project edges to reduce clutter. *[default: False]*
* `--html`: Write a self-contained interactive HTML graph to this path.
* `--dot`: Write a Graphviz DOT file to this path.
* `--open-browser, --no-open-browser`: Open the generated HTML in the default browser (requires --html). *[default: False]*

## witan inject-context

```console
witan inject-context [OPTIONS]
```

Print workflow context for the UserPromptSubmit hook.

Emits active WorkflowProjects and ready Tasks for the current git repo to
stdout. Designed to be called by ``~/.claude/hooks/workflow-context-inject.sh``
— always exits 0 and never blocks even when the graph is missing or the repo
is not in git.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `--debug, --no-debug`: and the reason for any swallowed failure) to stderr. stdout still carries
    only the injected block, so ``witan inject-context --debug`` is safe to
    run by hand to see why the block is blank. *[default: False]*

## witan session-checkpoint

```console
witan session-checkpoint
```

Auto-close the active WorkflowSession on agent stop (Stop hook).

Reads the session handle ``workflow_session_start`` returned (persisted
locally, see ``witan.session_state``) and passes its ``session_slug`` back to
``workflow_session_end``. No-op when there is no handle — the session was
already closed explicitly. Always exits 0 and never blocks. Also
opportunistically triggers a throttled background store compaction.

The end call goes through ``_srv()``, so it reaches whichever server actually
holds the session: the in-process module locally, or the deployment over MCP.
Writing straight to a local store here is what used to leave deployed
sessions open forever.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*

## witan optimize

```console
witan optimize [OPTIONS]
```

Compact the graph store's Lance fragments (non-destructive).

Collapses the many tiny fragments that accrue from every write so opening
the store stays cheap. Safe to run repeatedly; takes the store write lock.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `--store`:

## witan cleanup

```console
witan cleanup [OPTIONS]
```

Remove old Lance versions to reclaim disk (**destructive**).

``optimize`` compacts fragments but leaves old versions behind; this GCs
them, keeping the most recent ``keep`` versions per table (and/or those
newer than ``older_than``). Irreversible, so it requires ``--yes``.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `--store`:
* `--keep`: *[default: 10]*
* `--older-than`:
* `--yes, --no-yes`: *[default: False]*

## witan memory

```console
witan memory [OPTIONS] [ARGS]
```

Search memory (BM25), or with no query list memories (filtered by --kind).

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `QUERY, --query`:
* `--kind`: *[choices: pattern, project_fact, lesson, agent_context]*
* `--repo`:
* `--all-repos, --no-all-repos`: *[default: False]*
* `--limit`: *[default: 20]*

## witan projects

```console
witan projects [OPTIONS]
```

List workflow projects (default: active in the current repo).

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `--repo`:
* `--status`: *[default: active]*
* `--all-repos, --no-all-repos`: *[default: False]*
* `--limit`: *[default: 50]*

## witan project

```console
witan project COMMAND SLUG
```

Manage workflow projects.

**Commands**:

* [`advance`](#witan-project-advance): Advance a project to a new phase.
* [`block`](#witan-project-block): Declare that ``slug`` must complete before ``blocks`` can begin.
* [`complete`](#witan-project-complete): Complete a project and seal its immutable corpus trace.
* [`create`](#witan-project-create): Create a new workflow project.
* [`run`](#witan-project-run): Launch an agent session focused on a workflow project.
* [`status`](#witan-project-status): Resume view — phase, ready tasks, last session, blockers ("what next").
* [`tasks`](#witan-project-tasks): List a project's tasks, optionally with their dependency structure.
* [`unblock`](#witan-project-unblock): Remove a project dependency declared with ``project block``.
* [`update`](#witan-project-update): Correct a project's metadata after creation.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `SLUG, --slug`: **[required]**

### witan project status

```console
witan project status [OPTIONS] SLUG
```

Resume view — phase, ready tasks, last session, blockers ("what next").

The single-call resume view for a project. Pass ``--json`` for the raw
``workflow_project_status`` payload.

**Parameters**:

* `SLUG, --slug`: **[required]**
* `--json, --no-json`: *[default: False]*

### witan project tasks

```console
witan project tasks [OPTIONS] SLUG
```

List a project's tasks, optionally with their dependency structure.

``project <slug>`` already shows a flat task list; this focuses on the tasks
and, with ``--detail``, expands each task's blockers (what it waits on) and
dependents (what waits on it), resolving statuses from the project's own task
set so the dependency chain is visible without hopping between commands.

**Parameters**:

* `SLUG, --slug`: **[required]**
* `--status`:
* `--detail, --no-detail`: *[default: False]*

### witan project create

```console
witan project create [OPTIONS] TITLE
```

Create a new workflow project.

**Parameters**:

* `TITLE, --title`: **[required]**
* `--description`: *[default: ""]*
* `--phase`: *[choices: discovery, spec, implementation, delivery]* *[default: discovery]*
* `--repo`:
* `--github-issue`:
* `--tags, --empty-tags`:

### witan project update

```console
witan project update [OPTIONS] SLUG
```

Correct a project's metadata after creation.

Only what you pass is touched, so this can never blank a field by accident.

The common case is repos: a project's real blast radius is rarely known
during discovery, and until the set is right, the project doesn't surface
in the injected context of the repos where the work actually lands.

Two things this deliberately can't do, matching the MCP tool. It can't set
the phase — ``project advance`` stays the only route, so a transition is
always seen by its ordering check (it allows going backwards, which is how
a phase set in error gets corrected). And it can't complete a project:
``--status`` takes ``active`` or ``abandoned``, but ``completed`` belongs to
``project complete``, which seals a corpus trace. Nothing should mint a
trace without a narrative.

**Parameters**:

* `SLUG, --slug`: **[required]**
* `--title`:
* `--description`:
* `--repos, --empty-repos`:
* `--add-repo, --empty-add-repo`:
* `--remove-repo, --empty-remove-repo`: after additions.
* `--tags, --empty-tags`:
* `--github-issue`:
* `--status`:

### witan project advance

```console
witan project advance --phase LITERAL[DISCOVERY, SPEC, IMPLEMENTATION, DELIVERY] [OPTIONS] SLUG
```

Advance a project to a new phase.

A backward or skip transition is not blocked from the CLI (elicitation is
only available in an MCP session), but the resulting ``advisory`` note is
surfaced so an unusual transition is still visible.

**Parameters**:

* `SLUG, --slug`: **[required]**
* `--phase`: **[required]** *[choices: discovery, spec, implementation, delivery]*
* `--github-pr`:

### witan project complete

```console
witan project complete --outcome STR [OPTIONS] SLUG
```

Complete a project and seal its immutable corpus trace.

**Parameters**:

* `SLUG, --slug`: **[required]**
* `--outcome`: **[required]**
* `--github-pr`:

### witan project block

```console
witan project block SLUG BLOCKS
```

Declare that ``slug`` must complete before ``blocks`` can begin.

**Parameters**:

* `SLUG, --slug`: **[required]**
* `BLOCKS, --blocks`: **[required]**

### witan project unblock

```console
witan project unblock SLUG BLOCKS
```

Remove a project dependency declared with ``project block``.

**Parameters**:

* `SLUG, --slug`: **[required]**
* `BLOCKS, --blocks`: **[required]**

### witan project run

```console
witan project run [OPTIONS] [ARGS]
```

Launch an agent session focused on a workflow project.

Without a slug, shows an interactive picker of active projects. Multiple
selections offer a choice between a consolidated single-session prompt or
running each project sequentially in separate agent invocations.

**Parameters**:

* `SLUG, --slug`:
* `--target`:
* `--agent`:
* `--model`:
* `--dry-run, --no-dry-run`: *[default: False]*
* `--repo`:
* `--all-repos, --no-all-repos`: *[default: False]*

## witan scan

Introspect and dry-run write-path content scanning (ADR 0001).

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*

### witan scan test

```console
witan scan test [OPTIONS] TEXT
```

Dry-run active detectors against TEXT and print findings. Nothing is written.

Runs the exact same :class:`~witan.scan.ScannerRegistry` the write path
uses, so a clean run here means the write path will accept ``text``
unchanged. Findings are reported with their secret-free preview only —
the matched text is never printed.

**Parameters**:

* `TEXT, --text`: **[required]**
* `--field`: e.g. skipping ``author``). *[default: content]*
* `--node-type`: *[default: Memory]*

### witan scan rules

```console
witan scan rules
```

List active detectors: category, source, and enforcement mode.

## witan session

Manage workflow sessions.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*

### witan session start

```console
witan session start --phase LITERAL[DISCOVERY, SPEC, IMPLEMENTATION, DELIVERY] [OPTIONS] PROJECT-SLUG
```

Link a session to a workflow project.

**Parameters**:

* `PROJECT-SLUG, --project-slug`: **[required]**
* `--phase`: **[required]** *[choices: discovery, spec, implementation, delivery]*
* `--session-id`: generated uuid). The Stop hook keys its state file on this.
* `--repo`:
* `--tags, --empty-tags`:

### witan session end

```console
witan session end --summary STR [OPTIONS] SESSION-SLUG
```

Close a session with a handoff summary.

**Parameters**:

* `SESSION-SLUG, --session-slug`: **[required]**
* `--summary`: **[required]**
* `--tools-used, --empty-tools-used`:
* `--files-changed, --empty-files-changed`:

### witan session sweep

```console
witan session sweep [OPTIONS]
```

Close sessions that leaked open.

A session with no ``ended_at`` is not cosmetic: ``project complete`` folds
every linked session into the corpus trace, so a leaked one inflates
``session_count``, contributes its phase having recorded nothing, carries no
handoff summary, and cannot extend ``duration`` (computed from
``max(ended_at)``). It also drives the context hook's "N sessions in
<phase>" staleness nag on a project whose phase is progressing fine.

Dry-run by default — prints what it would close. Pass ``--yes`` to do it.
Closing an already-closed session just re-stamps ``ended_at``, so re-running
is harmless.

Against a deployment the per-actor client scopes the listing to the calling
user, so a sweep cannot reach a teammate's sessions.

**Parameters**:

* `--older-than`: Guards against closing a session that is legitimately running right now. *[default: 6h]*
* `--project`:
* `--yes, --no-yes`: *[default: False]*

### witan session list

```console
witan session list PROJECT-SLUG
```

List a project's sessions, newest last.

**Parameters**:

* `PROJECT-SLUG, --project-slug`: **[required]**

## witan setup

```console
witan setup [OPTIONS]
```

Install witan for one or all supported coding agents.

Installs the omnigraph binary to ``~/.local/bin/``, writes a starter
``config.toml`` if one doesn't exist yet, copies bundled skills and
hooks/extensions to the agent's config directories, and merges the witan
MCP server entry into the agent's config file. When witan-code is also
installed (importable in this environment — e.g. via ``--with`` in the
MCP server's uvx invocation), its skill and hooks (registered as
``witan code …``, not a separate ``witan-code`` binary) are folded into
the same install pass — no separate MCP entry, since ``witan serve``
already mounts witan-code's tools in-process. A single ``witan setup``
then covers both packages; otherwise install witan-code separately with
``witan-code setup`` (or the mounted ``witan code setup``).

Re-run after every upgrade to refresh installed files.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `--agent`: pending a config-path verification fix — tracked separately.) *[choices: claude, pi, copilot, opencode, all]* *[default: claude]*
* `--author`:
* `--dry-run, --no-dry-run`: *[default: False]*

## witan target

Register and inspect named [targets.*] blocks (deployed witan endpoints).

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*

### witan target add

```console
witan target add [OPTIONS] NAME
```

Register a named target — a deployed witan endpoint, or a local store.

Writes a ``[targets.<name>]`` block to the config file in effect
(``WITAN_CONFIG``, else ``~/.config/witan/config.toml``), creating a
starter config first if none exists. Comments in an existing file are
preserved: the block is appended, not re-serialised.

Joining a deployment is then::

    witan target add hosted \
        --remote-url https://witan.example.org/mcp \
        --oidc-issuer https://sso.example.org/realms/ol-platform-engineering \
        --match-orgs my-org
    witan login --target hosted
    witan whoami --target hosted

Passing ``--match-orgs``/``--match-repos``/``--match-hosts``/``--match-paths``
lets the target select itself for matching checkouts, so ``--target`` is not
needed after the first time. Without any of them the target is only ever
reached explicitly (``--target``/``WITAN_TARGET``).

**Parameters**:

* `NAME, --name`: **[required]**
* `--remote-url`:
* `--oidc-issuer`:
* `--oidc-client-id`:
* `--oidc-audience`:
* `--server`:
* `--graph`:
* `--author`:
* `--agent`:
* `--match-orgs, --empty-match-orgs`:
* `--match-repos, --empty-match-repos`:
* `--match-hosts, --empty-match-hosts`:
* `--match-paths, --empty-match-paths`:
* `--force, --no-force`: *[default: False]*
* `--verify, --no-verify`: *[default: True]*
* `--login, --no-login`: *[default: False]*
* `--dry-run, --no-dry-run`: *[default: False]*

### witan target list

```console
witan target list
```

List configured targets, marking the one in effect here with ``*``.

### witan target remove

```console
witan target remove [OPTIONS] NAME
```

Delete a ``[targets.<name>]`` block from the config file.

**Parameters**:

* `NAME, --name`: **[required]**
* `--dry-run, --no-dry-run`: *[default: False]*

## witan tasks

```console
witan tasks [OPTIONS]
```

List tasks for the current repo (or filtered).

Closed tasks are elided by default — the list is a working view of live work.
Pass ``--status closed`` to see them (or any other status to filter to it).

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `--repo`:
* `--status`: non-closed statuses.
* `--project`:
* `--assignee`:
* `--ready, --no-ready`: *[default: False]*
* `--all-repos, --no-all-repos`: *[default: False]*
* `--limit`: *[default: 50]*

## witan task

```console
witan task COMMAND SLUG
```

Manage tasks.

**Commands**:

* [`claim`](#witan-task-claim): Claim a task for work (status in_progress, with a lease).
* [`close`](#witan-task-close): Close a task, recording an optional resolution.
* [`create`](#witan-task-create): Create a task in the work-coordination graph.
* [`link`](#witan-task-link): Link two tasks (or a task to a memory).
* [`release`](#witan-task-release): Release a claim, returning the task to ``open`` (or another status).
* [`run`](#witan-task-run): Claim one or more tasks and launch an agent to execute them.
* [`unlink`](#witan-task-unlink): Remove a link between two tasks (or a task and a memory).
* [`update`](#witan-task-update): Update a task's mutable fields (only provided fields change).

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `SLUG, --slug`: **[required]**

### witan task create

```console
witan task create [OPTIONS] TITLE
```

Create a task in the work-coordination graph.

**Parameters**:

* `TITLE, --title`: **[required]**
* `--description`: *[default: ""]*
* `--type`: *[choices: bug, feature, task, chore, epic]* *[default: task]*
* `--priority`: *[choices: p0, p1, p2, p3]* *[default: p2]*
* `--repo`:
* `--project`:
* `--parent`:
* `--blocked-by, --empty-blocked-by`:
* `--discovered-from, --empty-discovered-from`:
* `--external-uri`:
* `--symbol-refs, --empty-symbol-refs`:
* `--tags, --empty-tags`:

### witan task close

```console
witan task close [OPTIONS] SLUG
```

Close a task, recording an optional resolution.

Closing a blocker unblocks its dependents.

**Parameters**:

* `SLUG, --slug`: **[required]**
* `--resolution`:

### witan task claim

```console
witan task claim [OPTIONS] SLUG
```

Claim a task for work (status in_progress, with a lease).

A live claim held by someone else is refused unless ``--force`` is passed
(CLI has no interactive steal prompt).

**Parameters**:

* `SLUG, --slug`: **[required]**
* `--assignee`: session so parallel sessions don't share one claim).
* `--force, --no-force`: *[default: False]*

### witan task release

```console
witan task release [OPTIONS] SLUG
```

Release a claim, returning the task to ``open`` (or another status).

**Parameters**:

* `SLUG, --slug`: **[required]**
* `--assignee`: by this agent session — a claim taken by another of your own sessions
    still matches, since the check is on identity, not session).
* `--status`: *[choices: open, in_progress, blocked, closed]* *[default: open]*
* `--force, --no-force`: *[default: False]*

### witan task update

```console
witan task update [OPTIONS] SLUG
```

Update a task's mutable fields (only provided fields change).

To *close* a task prefer ``task close``; to *claim* it prefer ``task claim``;
to add dependencies use ``task link``.

**Parameters**:

* `SLUG, --slug`: **[required]**
* `--title`:
* `--description`:
* `--type`: *[choices: bug, feature, task, chore, epic]*
* `--priority`: *[choices: p0, p1, p2, p3]*
* `--status`: *[choices: open, in_progress, blocked, closed]*
* `--repo`:
* `--project`:
* `--parent`:
* `--assignee`:
* `--external-uri`:
* `--tags, --empty-tags`:

### witan task link

```console
witan task link FROM-SLUG TO-SLUG KIND
```

Link two tasks (or a task to a memory).

``from``/``to`` meaning depends on ``kind``:
blocks — from blocks to; parent — from is parent of to;
discovered_from — from was discovered from to; addresses — from addresses
memory to.

**Parameters**:

* `FROM-SLUG, --from-slug`: **[required]**
* `TO-SLUG, --to-slug`: **[required]**
* `KIND, --kind`: **[required]** *[choices: blocks, parent, discovered_from, addresses]*

### witan task unlink

```console
witan task unlink FROM-SLUG TO-SLUG KIND
```

Remove a link between two tasks (or a task and a memory).

The inverse of ``link``, with the same ``from``/``to`` meanings. Use it
when a link was recorded backwards or against the wrong slug; removing a
``blocks`` link is how a wrongly-blocked task becomes ready again.

Reports plainly when there was no such link — that is a no-op, not an
error, so re-running is safe.

**Parameters**:

* `FROM-SLUG, --from-slug`: **[required]**
* `TO-SLUG, --to-slug`: **[required]**
* `KIND, --kind`: **[required]** *[choices: blocks, parent, discovered_from, addresses]*

### witan task run

```console
witan task run [OPTIONS] [ARGS]
```

Claim one or more tasks and launch an agent to execute them.

Without a slug, shows an interactive picker of ready tasks. Multiple
selections offer a choice between a consolidated single-session prompt or
running each task sequentially in separate agent invocations.

**Parameters**:

* `SLUG, --slug`:
* `--target`:
* `--agent`:
* `--model`:
* `--claim, --no-claim`: *[default: True]*
* `--force, --no-force`: this the command could report a task as held and offer no way past it
    from the CLI it was reported in — the interactive steal prompt is
    server-side and unreachable through ``_fn``, which passes no ``ctx``. *[default: False]*
* `--dry-run, --no-dry-run`: *[default: False]*
* `--repo`:
* `--all-repos, --no-all-repos`: *[default: False]*
* `--project`:

## witan traces

```console
witan traces [OPTIONS]
```

List corpus workflow traces (default: current repo).

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `--repo`:
* `--tags, --empty-tags`:
* `--author`:
* `--all-repos, --no-all-repos`: *[default: False]*
* `--limit`: *[default: 50]*

## witan trace

```console
witan trace COMMAND SLUG
```

Inspect corpus trace records.

**Commands**:

* [`list`](#witan-trace-list): List corpus workflow traces (alias of ``witan traces``).

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `SLUG, --slug`: **[required]**

### witan trace list

```console
witan trace list [OPTIONS]
```

List corpus workflow traces (alias of ``witan traces``).

**Parameters**:

* `--repo`:
* `--tags, --empty-tags`:
* `--author`:
* `--all-repos, --no-all-repos`: *[default: False]*
* `--limit`: *[default: 50]*

## witan migrate

One-shot, idempotent schema and data migrations.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*

### witan migrate schema

```console
witan migrate schema
```

Apply the bundled schema to the configured store (idempotent).

Reconciles an existing store with the current schema (new nodes/edges/fields).
Startup now does this on its own when ``schema.pg`` changes; this forces the
apply regardless of the mtime stamp.

### witan migrate storage

```console
witan migrate storage [OPTIONS] [ARGS]
```

Rebuild a local store stuck on an old, incompatible omnigraph format.

omnigraph uses strict single-version storage: a release that bumps the
internal on-disk schema (e.g. 0.7 → 0.8) refuses to open graphs an older
binary wrote. This detects that refusal against your configured store
and, using a still-installed pre-upgrade ``omnigraph`` binary, replays
the documented rebuild — export with the old binary, then ``init`` +
``load`` with the new one. Node/edge data, vectors, and blobs are
preserved; commit history and branches are not. The original store is
renamed ``<store>.pre-migrate`` rather than deleted.

No-op if the store already opens fine with the current binary. Only
handles local on-disk stores — s3:// and http(s):// stores are managed
externally and must be rebuilt by hand per omnigraph's upgrade docs.

**Parameters**:

* `OLD-BINARY, --old-binary`: Path to the omnigraph binary that last wrote this store. Auto-detected
    as the first ``omnigraph`` on PATH that isn't the one witan is
    currently using, if omitted.
* `--yes, --no-yes`: Skip the confirmation prompt. *[default: False]*

### witan migrate merge

```console
witan migrate merge [OPTIONS] [ARGS]
```

Merge another store's data into this store, newest-record-wins on collisions.

Implements docs/migration-runbook.md's export -> reconcile -> load
(--mode merge) path: for every node present in both stores (same type +
slug), keeps whichever has the newer timestamp instead of `omnigraph load
--mode merge`'s raw last-loaded-wins overwrite, which ignores content
entirely. Rows only in ``source`` are always added; rows only in the
target are left untouched. Repeatable — re-running against an
already-merged target loads nothing new.

Each merge records a per-side watermark for the pair of stores, so the next
one can name the nodes BOTH sides have written since — the case where
newest-record-wins is not resolving a stale value but discarding somebody's
edit. Nothing is auto-merged; the divergent slugs are reported for you to
reconcile. The first merge of a pair has no watermark and says so.

**Parameters**:

* `SOURCE, --source`: Store URI to merge from (local path, ``s3://``, ``file://``, or an
    ``http(s)://`` omnigraph-server), or the path to a *local*
    ``omnigraph export`` JSONL — anything ending ``.jsonl`` is read as an
    export rather than re-exported, and is never fetched remotely. Use the
    export form to merge a store from another machine: Lance embeds
    absolute paths, so a ``.omni`` directory cannot be copied, but its
    export can.
* `--from`: Named ``[targets.<name>]`` block to merge *from*, in place of
    ``source`` — its ``server`` is the store URI. A target carrying only a
    ``remote_url`` is refused: there is no remote-export path, so it has
    nothing to merge from.
* `--to`: Named ``[targets.<name>]`` block to merge *into*, in place of the
    ambient destination. Spells out on the command line what setting
    ``WITAN_TARGET`` does out of the environment: a target with a
    ``remote_url`` is merged into through that deployment (as you, over
    MCP), one with only a ``server`` into that store URI. Mutually
    exclusive with ``target``, which names a store rather than a target.
* `--target`: Store URI to merge into. Defaults to the configured store. Created
    automatically if it's a local path that doesn't exist yet. A deployed
    graph is ``http(s)://<host>:<port>/graphs/<graph-id>`` (or just the
    configured store, when running in-cluster). Unlike ``source``, a
    ``.jsonl`` target is refused rather than treated as a store: merging
    appends to a graph, and an export is a snapshot of one.
* `--dry-run, --no-dry-run`: Preview the reconciliation decision for every colliding slug without
    writing anything. *[default: False]*

### witan migrate topics

```console
witan migrate topics
```

Backfill Topic nodes from existing memory tags.

For every distinct memory ``tag``, upsert a ``Topic{kind:"topic"}`` and a
``Tagged`` edge. Safe to re-run — already-created topics and edges are
skipped. Fails fast if the Topic schema isn't applied yet.

### witan migrate repo-keys

```console
witan migrate repo-keys
```

Fold every stored repo key onto its canonical, case-folded form (#142).

``normalise`` now lowercases GitHub/GitLab repo keys, so a key written
before that fix may still carry the old case and silently drop out of
every repo-scoped read. Rewrites Task/Memory/WorkflowSession ``repo`` (and
their ``symbol_refs`` repo prefixes), WorkflowProject/WorkflowTrace
``repos`` lists, and CodeBranch (recreated under the canonical slug, the
stale row marked ``abandoned``). Idempotent — safe to re-run, and safe to
run on a store with nothing to fix. Does not touch the code graph
(witan-code); prints which repos need `witan-code reindex` instead.

### witan migrate dedupe-sessions

```console
witan migrate dedupe-sessions [OPTIONS]
```

Flag WorkflowSessions a pre-upsert ``workflow_session_start`` duplicated.

Reports overlapping sessions that share a ``session_id`` — the signature of
a hook retry or transport reconnect — and marks the ones carrying no
summary as ``superseded_by`` the surviving session, so trace assembly and
the context hook's counts stop double-counting them. Nothing is deleted.

Dry by default: prints what it would do and changes nothing until
``--apply``. Sessions that share a ``session_id`` but ran one after another
are left alone — one session id legitimately spans several working stints.
Runs where every member wrote a real summary are reported rather than
guessed at; resolve those with ``--supersede``.

Deliberately not part of ``migrate all``: unlike the other migrations this
one makes a judgment call about corpus content, so it should be read before
it's applied.

**Parameters**:

* `--apply, --no-apply`: Write the marks instead of only reporting them. *[default: False]*
* `--supersede, --empty-supersede`: ``<duplicate-slug>=<survivor-slug>`` pairs to mark regardless of the
    automatic rule. Repeatable.

### witan migrate all

```console
witan migrate all
```

Run the full bring-up: apply schema, backfill topics, fold repo keys.

All three steps are idempotent, so this is safe to re-run — including as
part of every deploy, to keep a live store self-healing.

### witan migrate claim-authorship

```console
witan migrate claim-authorship [OPTIONS] [ARGS]
```

Take ownership of rows an earlier migration left under your local name.

A local store writes ``author`` from ``WITAN_AUTHOR`` / git ``user.name`` /
``$USER``; a deployment resolves it from your token's
``preferred_username``. The two never converge, so before this was fixed
every row you migrated kept a name your deployed identity cannot match —
and ``memory_delete`` refuses anyone but the author, permanently (#267).

``witan migrate merge`` now claims rows as they arrive, so this is only
needed for a store merged before that landed. Re-merging will not fix
those: reconciliation is newest-record-wins, and a re-sent row loses to its
own already-applied copy.

Dry by default. Run ``witan whoami`` first if you are unsure which identity
you are claiming *to*.

**Parameters**:

* `WAS, --was`: The author string the rows currently carry. Defaults to this machine's
    configured local author, which is the right answer when you are
    repairing your own cutover from this same checkout.
* `--apply, --no-apply`: Write the change instead of only reporting it. *[default: False]*

## witan code

witan-code — tree-sitter code graph + cross-repo bridge.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*

### witan code index

```console
witan code index [ARGS]
```

Incrementally index PATH (file or directory). Unchanged files are skipped.

**Parameters**:

* `PATH, --path`: *[default: .]*

### witan code reindex

```console
witan code reindex [OPTIONS] [ARGS]
```

Force re-index PATH, ignoring content hashes.

**Parameters**:

* `PATH, --path`: *[default: .]*
* `--rebuild, --no-rebuild`: Delete this repo's code graph, and the shared bridge graph if it is
    also unreadable, before indexing — the recovery for a store the
    installed omnigraph cannot open (`witan-code doctor`). A code graph is
    derived from the checkout, so this rebuilds it from source and needs no
    pre-upgrade binary; the deleted store is not backed up, because a copy
    nothing installed can read is just disk. Dropping the bridge costs
    every OTHER repo's cross-repo bindings until each is reindexed too. *[default: False]*
* `--yes, --no-yes`: Skip the confirmation prompt for ``--rebuild``. *[default: False]*

### witan code doctor

```console
witan code doctor
```

Check that every code graph — per-repo and the shared bridge — can be read.

Exits non-zero when any store is unreadable, so it is usable as a check.

### witan code deps

```console
witan code deps [ARGS]
```

Visualize cross-repo dependencies from the shared bridge store.

Prints a Rich summary of "repo A depends on repo B" links (A consumes a
contract B provides). Pass --html PATH to also emit an interactive graph.

**Parameters**:

* `KIND, --kind`: Filter to one contract kind (env_var/package/service/endpoint). *[choices: env_var, endpoint, package, service]*
* `REPO, --repo`: Keep only links touching a repo whose slug contains this substring.
* `HTML, --html`: Write a self-contained interactive HTML graph to this path.
* `OPEN-BROWSER, --open-browser, --no-open-browser`: Open the generated HTML in the default browser. *[default: False]*
* `MIN-PRECISION, --min-precision`: Minimum edge precision tier (docs/EDGE_PRECISION_TIERS.md). Default
    `heuristic` preserves prior behavior (every consumer/provider link
    this command has always shown). `precise` keeps only edges also
    covered by a Stage-2 canonical-symbol join — see `witan code stitch`. *[choices: precise, heuristic, fuzzy]* *[default: heuristic]*

### witan code symbols

```console
witan code symbols [ARGS]
```

Print a repo's symbol table from the bridge store (docs/SYMBOL_TABLE.md).

One row per (role, symbol): `exported` rows are the repo's public contract
surface; `external` rows are unresolved references Stage 2 joins against
other repos' exports.

**Parameters**:

* `REPO, --repo`: Canonical repo URI. Defaults to the repo detected from the CWD.
* `ROLE, --role`: Filter to exported or external rows. *[choices: exported, external]*
* `SCHEME, --scheme`: Filter to one symbol scheme (http/env/pkg/svc).

### witan code stitch

```console
witan code stitch [OPTIONS] [ARGS]
```

Print Stage-2 precise cross-repo edges from the bridge store (docs/SYMBOL_TABLE.md).

Joins every repo's unresolved external symbols against other repos'
exported symbols by canonical symbol string — distinct from the coarser
`witan code deps` heuristic (kind, key_norm) grouping.

**Parameters**:

* `REPO, --repo`: Keep only edges/gaps touching this repo. Omit to see the whole store.
* `--unresolved, --no-unresolved`: Print external references with no precise match instead of edges —
    gaps in indexing coverage (a provider isn't indexed yet, or none
    exists in this SOA). *[default: False]*

### witan code inject-context

```console
witan code inject-context
```

Print a short code-graph status block for the UserPromptSubmit hook.

Registered as the bare ``UserPromptSubmit`` hook command; always exits 0
and prints nothing when there's no store or in-flight index for the
current repo.

### witan code serve

```console
witan code serve
```

Run the code-graph MCP server standalone (code_* tools only).

When witan-code is mounted into the umbrella ``witan serve`` instead, that
command has already configured observability; the call here is idempotent so
the standalone path gets it too without double-configuring the combined one.

### witan code optimize

```console
witan code optimize [OPTIONS]
```

Compact a code-graph store's Lance fragments (non-destructive).

Collapses the many tiny fragments that accrue from every index/reindex so
opening the store stays cheap. Safe to run repeatedly; takes the store's
write lock.

**Parameters**:

* `--store`:
* `--bridge, --no-bridge`: *[default: False]*

### witan code cleanup

```console
witan code cleanup [OPTIONS]
```

Remove old Lance versions from a code-graph store (**destructive**).

``optimize`` compacts fragments but leaves old versions behind; this GCs
them, keeping the most recent ``keep`` versions per table (and/or those
newer than ``older_than``). Irreversible, so it requires ``--yes``.

**Parameters**:

* `--store`:
* `--bridge, --no-bridge`: *[default: False]*
* `--keep`: *[default: 10]*
* `--older-than`:
* `--yes, --no-yes`: *[default: False]*

### witan code reap-views

```console
witan code reap-views [OPTIONS]
```

Delete branch views nobody has written in a long time (**destructive**).

On a shared cluster graph every developer's every git branch gets a view of
its own, and nothing ever removes one — this is what bounds that. Views are
re-derivable caches, so a reaped view costs its owner a reindex, not work.

Distinct from ``branches --prune``, which asks whether *this checkout* still
has the git branch and so only makes sense against a store this machine
alone writes. This asks how long ago a view was last written, which a shared
graph can answer for every writer. A view with no writes of its own is never
reaped: it holds nothing, and there is no creation timestamp to age it by.

Reports by default; ``--apply`` is what deletes. On a shared graph deleting
requires ``WITAN_CODE_INDEX_ROLE=ci`` — Cedar grants ``branch_delete`` to
the CI indexer alone, and refusing here makes that a clear local error
rather than a server denial.

**Parameters**:

* `--store`: URL. Default: every store this config resolves to (cluster graphs when
    ``code_server`` is set, else the local ones), the shared bridge
    included.
* `--graph`: encode one as ``.../graphs/<id>``.
* `--max-idle-days`: ``WITAN_CODE_VIEW_MAX_IDLE_DAYS``). ``0`` disables reaping.
* `--apply, --no-apply`: *[default: False]*

### witan code checkpoint

```console
witan code checkpoint
```

Opportunistically compact the current repo's store(s) (Stop hook).

Spawns a throttled, detached ``witan-code optimize`` for the current
repo's store and the shared bridge store, each at most once per
``WITAN_CODE_OPTIMIZE_INTERVAL``, if either exists and is due. Best-effort
and non-blocking: always exits 0 and never raises, so a maintenance
failure can't fail the Stop hook. Registered as the bare ``Stop`` hook
command; not usually run by hand.

A no-op against cluster graphs — ``maintenance.due()`` never fires for a
remote store, since compacting the shared storage root is the cluster's
job rather than every client's at the end of every session.

### witan code session-init

```console
witan code session-init
```

Seed/refresh the whole repo's code graph in the background (SessionStart hook).

Detached and non-blocking — returns immediately regardless of repo size.
A per-repo lock (shared with ``inject-context``'s "indexing in progress"
check) prevents overlapping sessions from indexing at once. Registered as
the bare ``SessionStart`` hook command; not usually run by hand.

### witan code reindex-hook

```console
witan code reindex-hook
```

Incrementally reindex the file named in stdin's hook JSON (PostToolUse hook).

Reads the Claude Code hook payload from stdin, extracts
``tool_input.file_path`` (or ``path``/``filename``), and reindexes it if
it exists and is a known source type — foreground and fast (one file), so
the agent sees the change land immediately. Best-effort: a missing or
malformed payload is a silent no-op. Registered as the bare
``PostToolUse`` (matcher ``Edit|Write``) hook command; not usually run by
hand.

### witan code setup

```console
witan code setup [OPTIONS]
```

Install witan-code for one or all supported coding agents.

Installs the omnigraph binary to ~/.local/bin/, copies the bundled skill
and Pi extension to the agent's config directories, registers the four
hooks (bare CLI commands — no wrapper scripts to copy), and merges the
witan-code MCP server entry into the agent's config file. Independent of
`witan setup` — running both is fine (each only touches its own entries);
running just this one is enough for a witan-code-only install.

Re-run after every upgrade to refresh installed files.

**Parameters**:

* `--agent`: *[choices: claude, pi, copilot, opencode, all]* *[default: claude]*
* `--author`:
* `--dry-run, --no-dry-run`: *[default: False]*

### witan code branches

```console
witan code branches [OPTIONS]
```

List the in-flight branch views per indexed repo store, and who owns each.

A non-default git branch is indexed onto its own view, named for its
writer as well as the branch (docs/BRANCH_INDEXING.md), so two checkouts
of the same branch do not overwrite each other. Views are re-derivable
caches, so lifecycle is deletion, not merge.

**Parameters**:

* `--branch`: Show only views of this git branch — every writer's, which is how you
    find a teammate's in-flight work. Pass a listed view name to
    ``--branch`` on the read commands to query it.
* `--prune, --no-prune`: Delete the CURRENT repo's views whose git branch no longer exists
    locally, plus the ``_detached`` scratch view. Other repos' stores are
    only listed (their git refs aren't visible from here). Local stores
    only, on both counts below: pruning reads this machine's git refs as
    the authority, which is true of a store only this machine writes and
    false of a shared cluster graph. *[default: False]*

### witan code repos

```console
witan code repos
```

List the repositories that have a code graph indexed.

### witan code login

```console
witan code login
```

Authenticate to the deployed witan service via the OIDC device grant.

Prints a verification URL and a user code; approve it in a browser, and the
resulting token is cached (mode 0600) and refreshed automatically for
subsequent `witan-code …` commands.

The cache is shared with the `witan` CLI and keyed by (issuer, client id),
so if you already ran `witan login` against the same deployment you do not
need this at all — and running it here also logs `witan` in.

### witan code logout

```console
witan code logout
```

Forget the cached token for the configured deployment.

The cache is shared with the `witan` CLI, so this logs both out.

### witan code whoami

```console
witan code whoami
```

Show the identity the CLI presents to the deployed witan service.

## witan serve

```console
witan serve [OPTIONS]
```

Run the witan MCP server.

Serves the work-coordination tools (memory_*, task_*, workflow_*) and, when
witan-code is installed, mounts the code-graph tools (code_*) into the same
server so a single MCP entry exposes everything.

Defaults to ``stdio`` for local per-user use (Claude Desktop, ``uvx``). Pass
``--transport streamable-http`` (or set ``WITAN_MCP_TRANSPORT``) to expose an
HTTP endpoint for a shared, deployed service — this is what ToolHive hosts.

The legacy HTTP+SSE transport is not offered: MCP 2026-07-28 deprecates it
with a 12-month offramp, and witan has no deployment on it to carry over.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `--transport`: ``http`` alias) binds a network listener. Env: ``WITAN_MCP_TRANSPORT``. *[choices: stdio, http, streamable-http]* *[env: WITAN_MCP_TRANSPORT]* *[default: stdio]*
* `--host`: Env: ``WITAN_MCP_HOST``. *[env: WITAN_MCP_HOST]* *[default: 127.0.0.1]*
* `--port`: *[env: WITAN_MCP_PORT]* *[default: 8000]*
* `--path`: Env: ``WITAN_MCP_PATH``. *[env: WITAN_MCP_PATH]* *[default: /mcp]*
* `--shutdown-grace-seconds`: SIGTERM before dropping them. FastMCP's own default is **2 seconds**,
    which silently truncates any deployment that expects a rollout to drain
    — a witan write has been measured at 27s. Set this to the deployment's
    termination grace period. Env:
    ``WITAN_MCP_SHUTDOWN_GRACE_SECONDS``. *[env: WITAN_MCP_SHUTDOWN_GRACE_SECONDS]* *[default: 120.0]*

## witan run

```console
witan run [OPTIONS] SLUG
```

Claim a task and launch an agent to execute it.

Claims the task (status in_progress, assignee = your author), then hands the
terminal to ``<agent>`` seeded with a prompt describing the work. Run from
the task's repo checkout so the agent has the right working directory.

**Parameters**:

* `--output-format`: projects, memory, traces, scan, and mounted witan-code tables. Values:
    txt | json | toml | yaml. Env: WITAN_OUTPUT_FORMAT. *[choices: txt, json, toml, yaml]* *[env: WITAN_OUTPUT_FORMAT]* *[default: txt]*
* `SLUG, --slug`: **[required]**
* `--target`: Also overridable via WITAN_TARGET env var.
* `--agent`: WITAN_AGENT env var and target/config-file default.
* `--model`: var and target/config-file default.
* `--claim, --no-claim`: *[default: True]*
* `--dry-run, --no-dry-run`: *[default: False]*
