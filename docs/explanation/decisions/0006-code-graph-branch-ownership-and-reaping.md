<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan/docs/adr/0006-code-graph-branch-ownership-and-reaping.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan/docs/adr/0006-code-graph-branch-ownership-and-reaping.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/adr/0006-code-graph-branch-ownership-and-reaping.md).

# 6. Code-graph branch-view ownership and reaping

- Status: Accepted
- Date: 2026-08-01
- Deciders: witan platform owners
- Tracking: task `tk-branch-cedar-gating-stale-code-graph-branch-reap-0c621c`, project `wp-witan-multi-user-service-deployment-dcf6ee`
- Supersedes: —
- Amends: `0002-witan-cedar-authorization-bundle.md` (D2's code-graph and bridge rule shapes)
- Related: `0004-keycloak-jwt-per-user-actor-mapping.md`; witan-code `docs/BRANCH_INDEXING.md`; task `tk-decide-where-a-developer-s-in-flight-code-graph--1cfbfa` (the decision this implements); ol-infrastructure `docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md`

## Context

Per-user code-graph branch views live **on the shared cluster graph**, not in
private stores (decided 2026-07-31): isolated agents seeing each other's
in-flight work as it happens is much of what the shared service is for. witan-code
therefore names every view for its writer — `act-<sub>/<branch>` on a per-repo
graph, `act-<sub>/<repo-slug>/<branch>` on the bridge (`witan_code/views.py`,
PR #164) — so two checkouts of `feature-x` no longer overwrite each other.

That decision leaves two questions this ADR settles: **who the authorization
layer can actually hold to that naming**, and **what bounds the resulting branch
sprawl**, since every developer's every git branch now gets a view on one graph
and nothing about indexing a branch ever unindexes it.

### Force — omnigraph 0.8.1 cannot express ownership

The naming scheme was designed on the expectation that a Cedar rule could gate
writes with `startsWith(branch, principal.actor + "/")`. **It cannot.** Verified
against the 0.8.1 binary:

- A bundle rule compiles to a bare
  `permit(principal in Omnigraph::Group::"…", action == Omnigraph::Action::"…",
  resource == …)`. There is no `when {}` clause in the generated Cedar at all.
- The bundle schema is exactly
  `{version, groups, protected_branches, rules{id, allow{actors{group}, actions,
  branch_scope, target_branch_scope}}}`. The only branch predicate is the
  three-valued `any | protected | unprotected` scope; there is no branch-name
  pattern, no principal attribute, and no raw-Cedar escape hatch.

So "write only views prefixed with your own actor id" is inexpressible at the
policy layer, in this version, by any arrangement of the bundle.

### Force — staleness is the only signal a shared graph has

`omnigraph branch list --json` returns **bare names**: no creation date, no
owner, no size. The only per-branch timestamp anywhere is in the commit log —
`commit list --branch <b> --json` returns each commit tagged with the
`manifest_branch` it landed on and a microsecond `created_at`. A branch that has
only inherited its fork point's commits has no commit tagged with itself, and
there is nothing else to date it by.

## Decision

### D1 — Cedar enforces main-vs-WIP; the client enforces writer-vs-writer

Two layers, with the split stated rather than implied:

- **Cedar** (`policy/code-graph.policy.yaml`, `policy/bridge.policy.yaml`):
  `main` is protected and writable only by `witan-ci`; every other branch is
  unprotected and writable by any authenticated `witan-users` member. This is
  the half that survives a lying client.
- **witan-code** (`graph.py:owns_view`): a process writes only views prefixed
  with its own actor id, and refuses otherwise with a message naming the owner.
  This is the half Cedar cannot express.

A client that ignores the write guard can still overwrite a colleague's view.
That is a **known, accepted v1 gap**, not an oversight: the alternative is
abandoning shared-graph visibility, which is the feature. It is pinned by
`tests/code-graph.tests.yaml:cedar-cannot-scope-wip-writes-to-owner`, which
asserts `expect: allow` for one user writing another's view — so an omnigraph
release that adds a branch-name or principal-attribute predicate **fails the
build** and forces the bundle to be tightened rather than letting the gap
persist unnoticed.

### D2 — Reads stay open across principals, deliberately

Any principal may read any other's branch view, on both the per-repo graphs and
the bridge. This is the point of putting views on the shared graph, so it is
asserted explicitly (`user-reads-another-users-view`,
`user-reads-another-users-bridge-view`) rather than left to follow from
`branch_scope: any` — a later tightening that scoped reads to the caller's own
views would defeat the decision, and should fail a test when it is attempted.

### D3 — `branch_delete` on a shared graph is CI's alone

Users get `branch_create` on unprotected branches and **not** `branch_delete`.
Cedar cannot scope a delete to the view's owner any more than it can scope a
write, and the two are not equally survivable: an overwrite is repaired by the
owner reindexing, a delete of the wrong view is the same repair plus the owner
having no idea why. No client path needs it — `witan-code branches --prune`
already refuses against a shared graph — so the grant is pure exposure.

This amends ADR 0002 D2, which gave users `branch_create`/`branch_delete`.

### D4 — The bridge is branched and user-writable on WIP branches

ADR 0002 D2 described the bridge as flat and read-only for humans. It is
neither: indexing a WIP git branch writes that branch's cross-repo bindings to
the bridge in the same pass (`witan_code/bridge.py`), on a repo-qualified view
of its own. Under the previous bundle every developer's WIP index would have
half-succeeded on the cluster — per-repo graph written, bridge bindings denied.
The bridge bundle now mirrors the code-graph one: `main` is the CI-owned
committed projection, every other branch is an unprotected per-user view.

### D5 — Reaping is server-side, idleness-based, and CI-owned

A stale-view reaper (`witan_code/reaper.py`, `witan-code reap-views`) deletes
views nobody has written in `WITAN_CODE_VIEW_MAX_IDLE_DAYS` (default 14). It is
deliberately **not** `branches --prune` with a wider scope: that command asks
whether *this checkout* still has the git branch, which is a sound question
about a store one machine writes and a meaningless one about a store every user
of the cluster writes — from a client, "I don't have that branch" and "that
branch is gone" are the same observation. It keeps refusing on shared graphs.

Two rules follow from the force above, and both are asserted:

- **`main` is never reaped**, however idle. It is the committed index every
  reader falls back to and is idle by design between merges.
- **A view with no commits of its own is never reaped.** It holds nothing that
  isn't already on its fork point, so deleting it reclaims nothing — and with no
  creation timestamp, one created ten seconds ago is indistinguishable from one
  created a year ago, so reaping it would race the indexer that just made it.

The reaper reports by default and deletes only under `--apply`, and refuses to
delete from a shared graph unless `WITAN_CODE_INDEX_ROLE=ci` — the client-side
mirror of D3, so an operator gets a clear local error instead of a Cedar denial.

## Consequences

- Per-writer isolation is real but **client-enforced**; the deployment's threat
  model must say so. Against a hostile client, the guarantee is main-vs-WIP.
- Branch sprawl is bounded by a scheduled job, not by any client action. If the
  reaper does not run, nothing else removes a view — ol-infrastructure owns
  scheduling it (`tk-omnigraph-maintenance-cronjob-scheduled-optimize-4321f8`).
- Idleness is not abandonment: a branch parked past the window and picked back
  up loses its view, not its work — the next index rebuilds it from the
  checkout. This is only acceptable because views are re-derivable caches, which
  is also why their lifecycle is deletion rather than merge.
- Never-written views accumulate unbounded, since nothing ages them. They cost
  a name in `branch list` and no storage. Revisit if omnigraph adds a branch
  creation timestamp.
- The reaper's staleness signal depends on `commit list --branch` reporting
  `manifest_branch` and `created_at`. That is an unversioned CLI shape, so it is
  asserted against the real binary in `tests/test_reaper.py` rather than mocked.
