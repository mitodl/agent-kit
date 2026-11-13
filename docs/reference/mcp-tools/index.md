<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: the registered FastMCP tool objects
-->

# MCP tools

witan exposes **52 MCP tools** across four domains. A single `witan serve` mounts all of them, so one MCP entry in your agent's config gets you the whole surface.

| Domain | Tools | What it covers |
| --- | --- | --- |
| [Memory & recall](memory.md) | 10 | Recording durable knowledge in the shared graph, and reading it back. |
| [Tasks](tasks.md) | 12 | The work-coordination layer: what needs doing, what blocks what, and who holds which piece of work right now. |
| [Workflow projects & sessions](workflow.md) | 12 | Tracking an engineering objective across many agent sessions without an explicit hand-off. |
| [Code graph](code.md) | 18 | Exact symbol lookups, caller graphs, change-impact analysis, and cross-repo contract tracing, served from a tree-sitter index. |

## Server instructions

Sent to the agent once, when it connects. Parameter descriptions that say "see instructions" refer to this text.

```text
Team-wide, shared, persistent memory and work-coordination graph. PREFER storing durable, shareable knowledge here — project facts, patterns, lessons, decisions, and hand-off context — over your private built-in/session memory, so other agents, future sessions, and teammates can find it. Record what you learn with memory_store. Also tracks workflow projects, sessions, and tasks.

Loading context: call recall(query=…) — it composes every memory read (BM25 + graph expansion + superseded-pruning + re-ranking) and degrades to a plain search when the graph has no edges. Seed it with symbol_id, task, topic, contract, or memory instead of/along with query; those seeds also return a section about the seed itself:
  symbol_id=… — also the tasks attached to a code symbol
  topic=…     — also the Topic node (everything tagged to it, cross-repo)
  contract=…, contract_kind=… — also the code providing/consuming the key
  memory=…    — also its neighbours by edge kind and the code symbols it names
Pass hops=0, include_superseded=True and a high limit to list exactly what a seed resolves to. Reach for a narrower read only for a specific need:
  recall           — default; contextual load / "what do we know about X"
  memory_get       — one memory by slug
  memory_list      — browse all of one kind (no query); optional language
  memory_search    — plain BM25, no graph expansion
  memory_contradictions — every contradicting pair, no seed needed
  workflow_project_memories — memories a project produced

Repo scoping: every tool auto-detects the current repo from .git/config. Pass repo (a canonical URI like https://github.com/mitodl/ol-django) to override it. To span every repo, omit repo and pass all_repos=true; on memory_store, task_create and workflow_session_start that files the row under no repo (repo-scoped task reads include such tasks, repo-scoped memory reads do not include such memories). Never pass an empty repo: it is refused. When memory_list detects no repo and none is passed, it returns slim records (slug, kind, title, tags — no content); memory_get a slug for the full memory.

Shared parameters:
  session_id: your agent session id: $CLAUDE_SESSION_ID, or under Pi and Codex the value of `echo $PI_SESSION_ID` / `echo $CODEX_SESSION_ID` (they do not pass it to MCP servers); on any other client, a stable unique string for the session. witan's CLI proxy fills it in and a local stdio server falls back to its own environment; pass it yourself when calling a deployed witan. On reads it is only logged.
  session_slug: the ws- handle workflow_session_start returned, not the agent session id. Records which session produced a write; a local stdio server finds it itself.
  assignee: a holder identity. Defaults to you, qualified by session_id so your parallel sessions stay distinct. As a filter, "@me" is every session of the calling identity.
  symbol_id / symbol_refs: witan-code symbol ids, repo#path/to/file.py::Name.
  Slug prefixes: tk- task, wp- project, ws- session, wt- trace, tp- topic; pat-/pf-/les-/ctx- memory (pattern, project_fact, lesson, agent_context).

Shared result fields:
  ui_url: against a deployment that serves the web UI, the link that opens a task or project there. Hand a person that link rather than building one.
  live_pr: when witan runs on your machine and a task's external_uri or a project's github_pr is a GitHub PR or issue, its state fetched just now, or {"error": …}. Where it disagrees with the description or comments, live_pr is current.
  lease_expired: on an in_progress task: its claim lapsed, so task_ready offers it for reclaim.

Task writes are compare-and-swap, fenced on the graph head and retried. Running out of attempts raises WriteContention with nothing written; retrying is safe. A call whose outcome is unknown (a timeout, a lost response) may still have landed, so re-read with task_get before retrying it.

Changing memory — pick by what is actually wrong:
  a field is wrong (wrong repo, typo'd title) → memory_update(slug, …); only the fields you pass change
  the knowledge changed → memory_store the new one, then memory_link(from_slug=<new>, to_slug=<old>, kind="supersedes") — the old one is hidden from default reads but kept (include_superseded=True to see it)
  it should never have existed (accidental duplicate, test write) → memory_delete(slug, confirm=True); author-only, hard delete
  it contains a secret → rotate the credential. memory_delete removes it from the current graph but not from history, and no tool can erase that.

Found a problem with someone else's task? task_comment(slug, body) leaves an attributed, append-only note ON it. Do NOT reach for task_update (it overwrites their description) or task_create (it puts an item that is not work into everyone's ready-work list). Comments come back in task_get, so read them before executing a task you claimed — they are often a correction to its own description.

Naming: the task_* tools track work items (task_create, task_claim, task_ready, …) and have nothing to do with MCP's own tasks/* extension for long-running calls. A task_* slug is a unit of work someone is assigned; an MCP task id is a handle on a call still executing.

Errors: a lookup that finds nothing returns null/empty, never raises; an invalid-but-well-formed mutation (self-link, claim contention) returns a status object with a reason; only malformed input raises.

Tree-sitter code graph across one or many repositories: resolve symbol definitions, callers, callees, references, and change-impact, and trace cross-repo contracts (env vars, endpoints, packages, services).

Symbol ids have the form `<repo>#<path/to/file.py>::<QualifiedName>` (a whole module is `<repo>#<path/to/file.py>::<module>`). Name-based tools return this id in the `symbol_id` field; pass it straight back to the id-routed tools (code_find_references / code_callers / code_impact / code_cross_repo_impact). Symbol ids compose with witan memory via soft references.

Repo scoping: tools auto-detect the current repo from `.git/config`. Pass `repo` (a canonical URI like `https://github.com/mitodl/ol-django`) to scope to one repo, or omit it when outside any repo to fan out across every indexed repo. An empty `repo` behaves like omitting it; it does not force a fan-out. A fan-out concatenates each store's BM25-ranked results rather than producing one global ordering.

Branch semantics: name-routed tools (code_find_definition, code_search_symbol, code_symbols_in_file) accept `branch` — either a git branch name, or a specific writer's view of one (`act-bob/feature_x`, as listed by code_indexed_branches). A branch has one view per writer, so reads prefer this process's own and fall back to another's; naming a view reads exactly that agent's in-flight work. The default follows the current checkout's branch ONLY when querying the current detected repo; when you pass `repo` for a different repo and omit `branch`, you read that store's default (main) view — pass `branch` explicitly to target another view. `symbol_id` does not encode a branch, so the id-routed tools follow the same default, reading the checkout's branch view when the id's repo is the current detected repo and that store's main view otherwise. code_impact and code_cross_repo_impact also accept `branch` for the id's repo (pass the default branch, e.g. `main`, to read main from a checkout whose own branch is indexed — where a symbol the branch removed no longer exists); code_find_references and code_callers take none.

min_precision (`heuristic` default | `precise`) on the interface and cross-repo tools: `precise` keeps only edges also confirmed by a canonical-symbol join, suppressing false positives.

Calls/References edges are heuristic (syntactic name resolution), not a precise call graph; code_find_references includes code_callers.
```
