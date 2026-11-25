<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan/docs/adr/0013-one-infra-graph-with-a-fixed-id.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan/docs/adr/0013-one-infra-graph-with-a-fixed-id.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/adr/0013-one-infra-graph-with-a-fixed-id.md).

# 13. witan-infra keeps the whole estate in one graph with the fixed id `infra`

- Status: Proposed
- Date: 2026-11-19
- Deciders: witan platform owners
- Tracking: task `tk-write-the-witan-infra-design-spec-docs-design-wi-3ef30c`,
  project `wp-witan-infra-digital-twin-of-running-infrastructu-544088`
- Related: `docs/internals/design/witan-infra-spec.md` (the design this
  topology serves); `docs/adr/0002-witan-cedar-authorization-bundle.md` (the
  bundle model the `infra` bundle follows); `docs/adr/0006-code-graph-branch-ownership-and-reaping.md`
  (why branch views exist for code graphs, and why infra has none)

## Context

witan-infra maps running infrastructure (IaC state, Kubernetes objects,
observed trace topology, cloud inventory) into an omnigraph graph and joins it
to the memory and code layers. Before any collector is written, the store has
to have a shape: how many graphs, with what ids, and with what views.

The code layer answers the same question with one graph per repository
(`code_graph_id(repo)` in `witan_core.graph_ids`) plus a shared `code-bridge`
graph, and per-user branch views for work in progress. Infra has different
forces.

### Forces

- The questions that matter cross environments and clouds: "is this the same
  service in QA and production", "which production services share this
  database", "what does this stack reference in another account". omnigraph has
  no cross-graph traversal, so any partition puts some of these out of reach of
  a single query.
- The estate is small for this engine. Counted from all 330 Pulumi stacks on
  2026-11-19: about 20.6k resources, 37.3k declared dependency edges and 14k
  config bindings: about 36k object nodes and, by estimate, on the order of
  100k edges.
  A full read of every `Resource` took 0.24 s locally at that size and 1.71 s at
  three times it. Against the deployed server, cost is dominated by round trips
  (a 4,100-row read 2.70 s, a 0-row read 2.54 s).
- The graph boundary is the authorization boundary. Cedar in omnigraph 0.15
  scopes authority to graph and branch only, with no node-type or row
  predicate. Sharding by environment would buy per-environment access control,
  but nobody has asked for that, and it would cost the cross-environment
  queries.
- An unbound graph is read-open: a graph declared in no policy bundle gets no
  policy, and any static-token actor may read it. Every graph id added is
  another binding that has to exist.
- A per-repo id scheme needs an id function shared by agent-kit and
  ol-infrastructure. That is now one import (`witan_core.graph_ids`), but a
  family of per-cluster or per-environment ids would be a second derived scheme
  where a constant does.
- Production has no private working copy. "What would this change do to
  production" is a `pulumi preview` question, not a branch of the graph.
- One writer: a scheduled reconciler running collectors in sequence. There are
  no concurrent indexers to separate.

## Decision

All infra data lives in one omnigraph graph whose id is the constant
`INFRA_GRAPH_ID = "infra"`, defined in `witan_core.graph_ids` beside
`BRIDGE_GRAPH_ID`. Environment, cloud, account, cluster and tenant are data (the
`environment` field and the recursive `Zone` node), not graph boundaries.

The graph has a single branch, `main`, declared protected. There are no branch
views and no per-user working copies. The reconciler writes `main` directly
with a merge-then-reap protocol, which keeps every scope readable during a
rewrite without needing branches for atomicity.

`infra` has its own Cedar bundle, bound whenever the graph is declared, with
reads limited to a dedicated reader group and writes to the ingester actor.

## Alternatives considered

- One graph per environment (`infra-production`, `infra-qa`, …). Gives
  per-environment authority and smaller graphs. Rejected: cross-environment
  questions become multi-query client joins, the graphs are already small, and
  an `environment` filter on one graph does the scoping reads need.
- One graph per cloud account or Kubernetes cluster. The only shape in which
  per-collector write authority would be a real Cedar boundary. Rejected for
  now: it fragments every service that spans clusters, and with one writer the
  authority it buys has nothing to protect against. If per-cluster collectors
  appear, they hand snapshots to the reconciler rather than writing a graph of
  their own.
- Infra node types inside the `code-bridge` graph. Avoids a new graph and
  bundle. Rejected: the bridge has many concurrent writers and a different
  reaping model, the bridge's readers are not the infra graph's intended
  readers, and the infra schema will change faster than the bridge's.
- Branch views as in the code layer. Rejected: there is no private infra state
  to stage, and a speculative change is answered by the IaC tool's preview.

## Consequences

- Any single query can span environments, accounts and clusters, up to the
  multi-hop limits of the query language.
- Read access is all or nothing across the estate. A reader sees every
  environment. Narrowing that later means a new graph and a migration, not a
  policy change.
- One bundle, one schema file, one `build_cluster_graphs` entry with a fixed
  id, and no per-repo loop in ol-infrastructure.
- If the estate grows past about 100k nodes, or a hard requirement for
  per-environment access control appears, this decision is revisited. The
  first signal would be bulk-read latency in `infra_coverage` or the
  reconciler's own timings.
