# Branch-aware indexing — omnigraph branches mirror git branches

Status: per-repo branching implemented (2026-07-05); bridge overlay
implemented (2026-07-06); CodeBranch↔task linking (witan, Layer 1)
implemented (2026-07-06)
Related: [SYMBOL_FORMAT.md](SYMBOL_FORMAT.md), [PACKAGE_MAP.md](PACKAGE_MAP.md)

Today every index write lands on the store's default branch regardless of the
git branch checked out: an agent working on a feature branch overwrites the
`main` view of the repo, and a second agent (or the same user in another
worktree) sees half-in-flight symbols with no way to tell. Omnigraph has
native branching (`branch create/list/delete/merge`, `--branch` on
query/mutate, `load --branch <b> --from main` fork-on-first-write), so the fix
is structural: **index a git branch onto the omnigraph branch of the same
name**.

## Per-repo stores

* Git default branch (`main`/`master`) → omnigraph `main` (unchanged).
* Any other git branch → omnigraph branch named `sanitize_branch(<git
  branch>)` on that repo's store, forked from `main` on first write
  (`load --branch <b> --from main`). The fork means the branch starts as a
  full copy of the `main` view; the incremental indexer then rewrites only
  the files that differ — exactly the delta the git branch carries.
* Branch detection: `git rev-parse --abbrev-ref HEAD` in `repo.py`
  (worktrees resolve per-worktree, which is precisely what parallel agents
  need). Detached HEAD indexes to `main` behavior? No — detached HEAD writes
  to a `_detached` scratch branch so it can never corrupt `main`.
* Reads (`code_*` MCP tools, CLI) default to the current checkout's branch,
  falling back to `main` when the branch doesn't exist in the store yet.
  Tools gain an optional `branch` parameter so an agent can inspect
  *another* agent's in-flight view explicitly — that is the cross-agent
  visibility payoff.

### Lifecycle

Branch stores are re-derivable caches, so lifecycle is deletion, not merge:

* When the git branch merges to the default branch, the post-merge index of
  `main` already reflects the result — the omnigraph branch is simply
  deleted. `omnigraph branch merge` is not used for index data (re-indexing
  is cheaper and always consistent; merging stale Lance rows is neither).
* `witan-code branches [--prune]` lists omnigraph branches per store and
  deletes those whose git branch no longer exists (checked against
  `git for-each-ref`), plus `_detached`. Pruning is a **local-store**
  operation on both counts: it is refused in remote MCP-client mode (ADR
  0005) and refused against a remote store (below). On a shared graph one
  machine's missing git ref is not evidence a branch is dead — it would
  delete another user's in-flight view.

## Who may write the shared default-branch view

On the deployed omnigraph cluster a per-repo code graph is **one graph for
the whole team**, so `main` — the view with no branch scoping, which every
reader falls back to — has no natural owner. It gets one explicitly: **CI
indexes the default branch, everyone else reads it.**

Writer authority is a **role, not a transport**. "Refuse writes when the
store is remote" cannot be applied unconditionally, because the CI indexer is
remote too and is the one actor that must write. So the role is declared:

```
WITAN_CODE_INDEX_ROLE=ci     # or index_role = "ci" on a [targets.<name>] block
```

Values are `client` (the default — reads the shared view, never writes it)
and `ci`. An unrecognized value is an error rather than a silent
demotion to `client`, which would leave the shared view frozen with nothing
to explain it.

What the role gates (`witan_code.graph.check_writable`,
`witan_code.indexer._may_purge`):

| Write | Local store | Shared graph, `client` | Shared graph, `ci` |
|---|---|---|---|
| default-branch (`main`) view | ✅ | ❌ refused | ✅ |
| branch view (`--branch <b>`) | ✅ | ✅ | ✅ |
| stale-file purge | ✅ | ❌ | ✅ |
| `branches --prune` | ✅ | ❌ | ❌ |

Branch views are exempt from the default-view gate: they are written through
a branch-scoped client, so in-flight work never lands on the view everyone
reads. Whether developer branch views belong on the shared graph at all
(namespaced per user, with reaping and Cedar gating) or stay in the local
store with results merged at query time is **still open** — see task
`tk-ci-owns-the-default-branch-code-graph-clients-re-9c90d6` item 3.

Local stores are unaffected by the role: they have one user, who is their
writer.

## Bridge store

Bridge bindings from an in-flight branch must not pollute the shared `main`
cross-repo view, but a branch view should still see every *other* repo's
`main` bindings. Omnigraph branch forking gives this overlay for free — the
subtlety is naming: branch names collide across repos (`feature-x` in two
repos), so bridge branches are **repo-qualified**:

```
bridge branch = <sanitized-repo-slug>/<sanitized-git-branch>
```

forked from the bridge `main`. Writes for repo R on git branch B go to that
branch (`bridge.write_bindings`'s `branch` parameter, repo-qualified
internally); `code_cross_repo_impact`/`code_interface_*` auto-detect the
current checkout's repo+branch and read `R/B` when it exists, else `main` —
so an agent working on branch B sees its own in-flight bindings overlaid on
everyone else's `main`. Because the branch is forked once (on first write)
rather than kept continuously in sync, an overlay's view of *other* repos
can go stale relative to their current `main` if they're reindexed while
this branch is still open — the same re-derivable-cache tradeoff already
accepted for per-repo `main` (see Lifecycle above); prune/re-fork on the
next index resolves it. Bridge branch pruning rides the same
`branches --prune` sweep. The CLI (`witan code deps`/`stitch`/`symbols`)
does not yet follow this — it always reads bridge `main`.

## Linking code branches to projects and tasks (witan graph)

Branch-aware stores answer "what does branch B look like"; the
work-coordination graph should answer "*why* does branch B exist and who is
on it". This linkage lives in **witan, not witan-code**: it is coordination
state that must be shared and durable, while witan-code stores are local
re-derivable caches that `branches --prune` may destroy at any time. The
coupling stays one-way via soft references — the same pattern as
`Task.symbol_refs` — with git as the shared vocabulary: `CodeBranch`
references the **raw git branch name** (`feature/new-api`), never
witan-code's sanitized omnigraph branch name (`feature_new-api`), which is a
storage detail that must not leak into the witan schema. Consumers sanitize
at the edge before calling `code_*` tools with `branch=…`. New node + edges
in the witan (Layer 1) schema:

```
node CodeBranch {
    slug: String @key        // "<repo URI>|<git branch>"
    repo: String @index
    branch: String @index
    status: enum(active, merged, abandoned) @index
    created_at: DateTime
    updated_at: DateTime
}

edge WorksOn:    CodeBranch -> Task
edge ForProject: CodeBranch -> WorkflowProject
```

* `workflow_session_start` records the session's git branch and upserts the
  CodeBranch + `ForProject` edge automatically — no manual bookkeeping.
* `task_claim` on a repo checkout upserts `WorksOn` from the current branch,
  so "which branch carries task X" and "which tasks are in flight on branch
  B" are single-hop queries.
* The context-injection hook can then surface, at session start: *"branch
  feature-x is linked to task tk-… (claimed by session s-…)"* — in-flight
  work becomes visible before an agent duplicates it.
* Status transitions ride the prune sweep: git branch gone + task closed →
  `merged`; git branch gone + task open → `abandoned` (a signal, not a
  cleanup).

## Implementation order

1. ✅ `OmnigraphClient` grows a `branch: str | None` (adds `--branch`, and
   `--from main` on `load`); `repo.py` grows `current_branch()`.
2. ✅ Per-repo indexer + `code_*` read tools honor the branch with
   `main` fallback; `witan-code branches --prune`.
3. ✅ Bridge writes/reads use repo-qualified branch overlay.
4. ✅ witan-graph `CodeBranch` node + `WorksOn`/`ForProject` edges, wired into
   `workflow_session_start` / `task_claim` / the context hook (lives in
   witan, not witan-code — see witan's README § Code Branch Tracking).
