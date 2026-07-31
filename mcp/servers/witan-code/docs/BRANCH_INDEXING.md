# Branch-aware indexing — omnigraph branches mirror git branches

Status: per-repo branching implemented (2026-07-05); bridge overlay
implemented (2026-07-06); CodeBranch↔task linking (witan, Layer 1)
implemented (2026-07-06); per-writer view namespacing implemented
(2026-07-31)
Related: [SYMBOL_FORMAT.md](SYMBOL_FORMAT.md), [PACKAGE_MAP.md](PACKAGE_MAP.md)

Today every index write lands on the store's default branch regardless of the
git branch checked out: an agent working on a feature branch overwrites the
`main` view of the repo, and a second agent (or the same user in another
worktree) sees half-in-flight symbols with no way to tell. Omnigraph has
native branching (`branch create/list/delete/merge`, `--branch` on
query/mutate, `load --branch <b> --from main` fork-on-first-write), so the fix
is structural: **index a git branch onto the omnigraph branch of the same
name**.

## Per-writer branch views

A git branch is not a unique key for an index. Two checkouts on `feature-x`
— two developers, one developer in two worktrees, an agent and its human —
are two working trees, and on a shared cluster graph a view named for the
branch alone means the second writer overwrites the first with a different
uncommitted state. The symptom is the class of confidently-wrong answer PR
\#157 fixed for nested checkouts: a symbol resolving to somebody else's WIP.

So the writer is part of the name. One scheme covers both stores
(`witan_code/views.py`):

```
per-repo graph:  [<actor>/]<branch>
bridge graph:    [<actor>/]<repo-slug>/<branch>
```

The actor comes first in both, so **ownership is a prefix**: "may I write
this view" is one string comparison, the stale-view reaper can sweep by
owner, and a Cedar rule gates writes with
`startsWith(branch, principal.actor + "/")` without knowing which store it is
looking at.

`<actor>` is the ADR-0004 `act-<sub>` id — the same derivation the deployed
server uses (`witan_core.identity.derive_actor_id`), resolved client-side
from the `witan login` session (`witan_code/identity.py`), never from
`$USER`. It is absent when this process has no identity to name, which is the
normal case for purely local use: existing local stores keep the names they
have, and indexing offline needs no login.

**Isolation and visibility are not in tension.** Each view has exactly one
writer; every view is readable by everyone. `code_indexed_branches(branch=…)`
(CLI: `witan code branches --branch <b>`) lists every writer's view of a git
branch, and any listed view name can be passed straight back as `branch=` to
`code_find_definition` / `code_search_symbol` / `code_symbols_in_file`. That
cross-agent visibility is why branch views live on the shared graph at all
(DECIDED, 2026-07-31) rather than in per-user local stores.

**Ownership gates both destructive operations.** `graph.owns_view` is the one
predicate behind both `check_writable` (may I write this view) and
`indexer._may_purge` (may I delete rows from it): a local store has one user
who is its writer; CI owns the shared default view
(`WITAN_CODE_INDEX_ROLE=ci`); every actor owns its own branch views. The
earlier rule — "remote and not the designated writer" — got the first two
right and the third wrong, refusing a developer the purge of their own view,
where files they had deleted therefore lingered.

## Per-repo stores

* Git default branch (`main`/`master`) → omnigraph `main` (unchanged).
* Any other git branch → the writer's view `[<actor>/]sanitize_branch(<git
  branch>)` on that repo's store, forked from `main` on first write
  (`load --branch <b> --from main`). The fork means the branch starts as a
  full copy of the `main` view; the incremental indexer then rewrites only
  the files that differ — exactly the delta the git branch carries.
* Branch detection: `git rev-parse --abbrev-ref HEAD` in `repo.py`
  (worktrees resolve per-worktree, which is precisely what parallel agents
  need). Detached HEAD indexes to `main` behavior? No — detached HEAD writes
  to a `_detached` scratch branch so it can never corrupt `main`.
* Reads (`code_*` MCP tools, CLI) default to the current checkout's branch —
  to *this* actor's view of it first, then any other writer's, then `main`.
  The fallback to a colleague's view is deliberate: before you have indexed a
  branch yourself, the closest thing to "the code on feature-x" is whatever
  view of it exists, and reading is not the operation that needs an owner.
  Tools take an optional `branch`, which accepts either a git branch name
  (resolved the same way) or a full view name like `act-bob/feature_x` to
  inspect one specific agent's in-flight view — that is the cross-agent
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
| own branch view (`act-me/<b>`) | ✅ | ✅ | ✅ |
| another actor's branch view | ✅ | ❌ refused | ❌ refused |
| stale-file purge | ✅ | ✅ own view only | ✅ |
| `branches --prune` | ✅ | ❌ | ❌ |

Local stores are unaffected by the role: they have one user, who is their
writer.

Note the role governs only the *default* view. Branch views are governed by
their name (§ Per-writer branch views): nobody, including CI, may write a
view prefixed with someone else's actor, and a branch view with no owner at
all is refused on a shared graph — that un-owned name is exactly the
collision this replaced.

### Per-user branch views live on the shared graph

**Decided (2026-07-31).** In-flight branch views go on the shared cluster
graph, not in a local store queried alongside it. Isolated agents being able
to see each other's work *as it is being made* — rather than after a merge —
is a large part of what the shared service is for, and that only works if
the views are somewhere every reader can reach.

Branch views are therefore exempt from the default-view *role* gate above: a
branch-scoped write cannot reach the view everyone falls back to, so it needs
no role. Anyone may write their own — and only their own.

Both things this decision required are **implemented** (2026-07-31):

1. **Branch views are namespaced per writer** — `[<actor>/]<branch>`, see
   § Per-writer branch views. Reads can enumerate every view of a git branch
   (`code_indexed_branches(branch=…)`) and query any of them by name, so
   isolation did not cost visibility.

2. **Stale-file purging follows view ownership.** `_may_purge` and
   `check_writable` share one predicate, `graph.owns_view`: "CI owns `main`"
   and "I own my own branch view" are its two shared-graph cases. A developer
   purging their own view is now allowed, which it was not while views had no
   single authoritative writer.

Reaping is consequently **server-side and mandatory**, not a client
convenience: branch sprawl is real under this decision, and no client can
tell whose branch views are still live — which is why `branches --prune` is
refused against a remote store above and stays that way. See
`tk-branch-cedar-gating-stale-code-graph-branch-reap-0c621c`.

## Bridge store

Bridge bindings from an in-flight branch must not pollute the shared `main`
cross-repo view, but a branch view should still see every *other* repo's
`main` bindings. Omnigraph branch forking gives this overlay for free — the
subtlety is naming: branch names collide across repos (`feature-x` in two
repos), so bridge branches are **repo-qualified**:

```
bridge branch = [<actor>/]<sanitized-repo-slug>/<sanitized-git-branch>
```

forked from the bridge `main`. The repo qualifier keeps `feature-x` in two
repos apart; the actor qualifier keeps `feature-x` in two *checkouts of one
repo* apart. Both are needed and they compose in that order — actor first, so
ownership stays a prefix of the name in both stores.

Writes for repo R on git branch B go to that branch
(`bridge.write_bindings`'s `branch`/`actor` parameters, composed internally by
`views.bridge_view`); `code_cross_repo_impact`/`code_interface_*` auto-detect
the current checkout's repo+branch and read this actor's overlay of `R/B`
when it exists, then any writer's, else `main` —
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

### From a task to the code on its branch

The two layers agree on the branch, and only on the branch — deliberately.
`CodeBranch` keys on `(repo, raw git branch)`; a code-graph view keys on
`(actor, sanitized branch)`. Neither holds the other's key, so "show me the
code as it stands on the branch carrying task X" is a mechanical two-step
rather than an edge across stores:

1. `task_get` → `CodeBranch.branch` (the raw name, e.g. `feature/new-api`).
2. `code_indexed_branches(branch="feature/new-api")` → every writer's view of
   it (`act-alice/feature_new-api`, …). Pass one back as `branch=` to any
   name-routed `code_*` tool.

Sanitizing is one-way, so the hop only works in this direction — which is the
direction that has an authority for the raw name. A `CodeBranch` may have
several views (one per writer on that branch, plus a possible un-owned local
one); when the task names a claimant, prefer that actor's view.

## Implementation order

1. ✅ `OmnigraphClient` grows a `branch: str | None` (adds `--branch`, and
   `--from main` on `load`); `repo.py` grows `current_branch()`.
2. ✅ Per-repo indexer + `code_*` read tools honor the branch with
   `main` fallback; `witan-code branches --prune`.
3. ✅ Bridge writes/reads use repo-qualified branch overlay.
4. ✅ witan-graph `CodeBranch` node + `WorksOn`/`ForProject` edges, wired into
   `workflow_session_start` / `task_claim` / the context hook (lives in
   witan, not witan-code — see witan's README § Code Branch Tracking).
5. ✅ Views namespaced per writer (`views.py`, `identity.py`); one ownership
   predicate behind `check_writable` and `_may_purge`;
   `code_indexed_branches(branch=…)` enumerates every writer's view.
