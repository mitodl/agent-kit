# 2. witan v1 Cedar authorization bundle

- Status: Accepted
- Date: 2026-07-07
- Deciders: witan platform owners
- Tracking: task `tk-witan-v1-cedar-policy-bundle-team-repo-scoped-re-77655e`, project `wp-witan-multi-user-service-deployment-dcf6ee`
- Supersedes: —
- Related: ol-infrastructure `docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md`; `tk-design-keycloak-jwt-omnigraph-per-user-actor-tok-728f0c` (per-user actors); `tk-spike-validate-omnigraph-server-remote-write-ser-1a8058` (spike that mapped the omnigraph policy surface)

## Context

Deployed multi-user witan (the sibling project) replaces local-per-user stores
with one shared omnigraph store served by `omnigraph-server`. A shared store
needs authorization: who may read, who may write, and to which part of the
graph. omnigraph ships a Cedar policy layer; this ADR fixes the **v1 bundle**
that agent-kit authors and CI validates, and the boundary with what
ol-infrastructure templates at deploy time.

### Forces — what omnigraph's Cedar layer can and cannot express

Confirmed against omnigraph `docs/user/operations/policy.md`, the
`omnigraph-policy` crate, and the 0.8.1 binary (`omnigraph policy validate/test`):

- **Allow-only.** Every rule is a `permit`; there is no `deny` key; ungranted ⇒
  denied. Restriction is expressed by omission.
- **Finest scope is graph + branch.** No per-node-type / per-row authority —
  it is "the query-layer's job" and unimplemented. Within one graph, `change`
  is all-or-nothing across node kinds.
- **Actor identity is server-resolved from the bearer token**, never from
  client-supplied fields.
- **Branch scoping** exists: `branch_scope` (source) for `read`/`export`/
  `change`; `target_branch_scope` (destination) for `schema_apply`/`branch_*`;
  values `any | protected | unprotected`; `protected_branches:` names branches.
- **Maintenance ops (`repair`/`optimize`/`cleanup`) are direct-storage-only**
  and cannot be Cedar-gated.
- **The offline CLI validates only per-graph bundles.** `policy validate/test`
  load bundles under the per-graph engine, reject the server-scoped `graph_list`
  action in a graph bundle, and fan a `[cluster]` bundle onto every graph
  (tripping "one bundle per graph scope"). So a server bundle cannot be
  exercised by the same offline harness as the per-graph bundles.
- **Identity is per-user, not per-team.** omnigraph's own `groups:` are built
  from individual `act-<person>` actors; role/team is an aggregation *over*
  per-user actors. Keycloak already issues each user a JWT with role/group
  claims. So per-user actors are the natural v1, grouped by Keycloak claim.

## Decision

### D1 — Two groups over per-user actors

`witan-users` (one `act-<sub>` per authenticated human, from the Keycloak claim)
and `witan-ci` (`act-svc-witan-ci`, the single legitimate non-human actor).
Group **names** are stable and identical across bundles; **membership** is
templated by ol-infrastructure from Keycloak claims, not committed.

### D2 — One bundle per graph scope, mapped to the layer topology

- **`memory.policy.yaml` → `[memory]`** (Layer 1, flat shared work graph):
  users read/export/change/invoke_query on the single main branch; `schema_apply`
  is `witan-ci`-only. No per-node-type rules — not expressible.
- **`code-graph.policy.yaml` → every per-repo code-graph id** (Layer 2): the
  repo's default git branch maps to store `main` = `protected`; all other git
  branches are `unprotected` WIP. Users read/invoke anywhere and change / create /
  delete only `unprotected` branches; `witan-ci` owns `change`, `branch_merge`,
  and `schema_apply` into protected `main`. WIP reindexes are isolated;
  promotion into `main` is deliberate and CI-owned.
- **`bridge.policy.yaml` → `[bridge]`** (Layer 2.5, derived cross-repo bridge):
  read-only for users; `witan-ci`-written.

### D3 — Server-level `graph_list` is a deploy-time bundle, not CI-harnessed

`server.policy.yaml` (`graph_list`, `applies_to: [cluster]`) grants graph
enumeration to both groups. Because the 0.8.1 offline CLI has no server-scope
validation path, it is applied and enforced by `omnigraph-server` at
boot/runtime and excluded from `check.sh`; `tests/server.tests.yaml` records the
intended decisions for when upstream adds a server-scope harness.

### D4 — Maintenance is gated by IAM, not Cedar

`repair`/`optimize`/`cleanup` cannot be Cedar-gated. Access is restricted with
AWS IAM on the backing bucket. There is no `svc-witan-admin` Cedar principal.

### D5 — CI validates and unit-tests the bundle against the real binary

`policy/check.sh` converges a fixture cluster (`policy/cluster.yaml`, stub
graphs `memory`/`code_example`/`bridge`) and runs `omnigraph policy validate`
plus 24 declarative `policy test` cases across the three per-graph bundles. It
runs as the `witan (Cedar policy bundle)` job in `witan-tests.yml`. The fixture
is a test harness; the deployed cluster.yaml is templated by ol-infrastructure.

## Consequences

- Read/write scoping is enforced at the graph+branch grain that omnigraph
  actually supports; the bundle makes no promise it cannot keep (no per-type
  rules, no Cedar-gated maintenance).
- WIP code-graph reindexes are isolated on unprotected branches per user;
  `main` is protected and CI-owned — the isolation goal of the branching-strategy
  work is satisfied by policy, not just convention.
- The agent-kit ↔ ol-infrastructure boundary is explicit: agent-kit owns the
  rule *shape* and its tests; ol-infrastructure owns graph enumeration, group
  membership, and IAM.
- Coarser-than-ideal Layer-1 authority (no per-node-type) is accepted for v1;
  finer control waits on an omnigraph query-layer capability, not a bundle
  change here.
