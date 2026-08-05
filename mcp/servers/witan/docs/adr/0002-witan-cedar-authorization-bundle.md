# 2. witan v1 Cedar authorization bundle

- Status: Accepted, amended by [0006](0006-code-graph-branch-ownership-and-reaping.md)
- Date: 2026-07-07
- Deciders: witan platform owners
- Tracking: task `tk-witan-v1-cedar-policy-bundle-team-repo-scoped-re-77655e`, project `wp-witan-multi-user-service-deployment-dcf6ee`
- Supersedes: —
- Related: ol-infrastructure `docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md`; `docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md` (how a request's `act-<sub>` and omnigraph bearer token are actually resolved); `tk-spike-validate-omnigraph-server-remote-write-ser-1a8058` (spike that mapped the omnigraph policy surface)

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

### D1 — Three groups: per-user humans + two distinct service accounts

*(Four as of the 2026-08-05 amendment at the end of this section.)*

- `witan-users` — one `act-<sub>` per authenticated human, from the Keycloak claim.
- `witan-ci` (`act-svc-witan-ci`) — the code-graph **data** pipeline
  (reindex-on-merge + WIP-branch lifecycle).
- `witan-service` (`act-svc-witan`) — the witan MCP service's **own** account.
  Schema definition/migration is a default, service-owned operation: the service
  applies the appropriate schema on every graph (`schema_apply`) as part of its
  boot/ownership duties. This is deliberately **not** the reindex pipeline and
  **not** a per-user permission — separating it keeps the pipeline unable to
  redefine schema, and keeps schema ownership in one place (the service) rather
  than smeared across a "CI" catch-all.

Group **names** are stable and identical across bundles; **membership** is
templated by ol-infrastructure (Keycloak claims for `witan-users`,
Vault-provisioned tokens for the service accounts), not committed.

**Amended 2026-08-05 — a fourth group, `witan-admin`.** See the amendment to D4
below: the break-glass maintenance principal of
[ADR 0005](0005-secure-cli-path-into-deployed-witan.md) path (b),
`act-svc-witan-admin`, needs Cedar rules after all, because the operations it
exists for (`witan migrate topics` / `repo-keys` / `merge`, a forced schema
apply) go through the **server**, not direct storage.

### D2 — One bundle per graph scope, mapped to the layer topology

- **`memory.policy.yaml` → `[memory]`** (Layer 1, flat shared work graph):
  users read/export/change/invoke_query on the single main branch; `witan-service`
  owns read + `schema_apply`. `witan-ci` has **no** role here (it is a code-graph
  actor). No per-node-type rules — not expressible.
- **`code-graph.policy.yaml` → every per-repo code-graph id** (Layer 2): the
  repo's default git branch maps to store `main` = `protected`; all other git
  branches are `unprotected` WIP. Users read/invoke anywhere and change / create /
  delete only `unprotected` branches; `witan-ci` owns `read`/`change` and
  `branch_merge` into protected `main` plus the unprotected WIP-branch lifecycle
  (but **not** `branch_delete` on `main` and **not** `schema_apply`);
  `witan-service` owns read + `schema_apply` on `main`. WIP reindexes are
  isolated; promotion into `main` is deliberate and CI-owned; schema stays
  service-owned.
  **Amended by [ADR 0006](0006-code-graph-branch-ownership-and-reaping.md) D3:**
  users keep `branch_create` but lose `branch_delete` — Cedar cannot scope a
  delete to the view's owner, so deletion on a shared graph is CI's alone.
- **`bridge.policy.yaml` → `[bridge]`** (Layer 2.5, derived cross-repo bridge):
  read-only for users; `witan-ci` writes the content; `witan-service` owns the
  schema.
  **Amended by [ADR 0006](0006-code-graph-branch-ownership-and-reaping.md) D4:**
  the bridge is neither flat nor read-only for humans — indexing a WIP git branch
  writes its cross-repo bindings there too, on a per-user view. Users get
  `change` + `branch_create` on unprotected branches; `main` stays CI's.

### D3 — Server-level `graph_list` is a deploy-time bundle, structurally linted

`server.policy.yaml` (`graph_list`, `applies_to: [cluster]`) grants graph
enumeration to all four groups (`witan-admin` added 2026-08-05 — see D1).
Because the 0.8.1 offline CLI has no server-scope
*semantic*-validation path (`policy validate`/`test` load under the per-graph
engine), it is applied and enforced by `omnigraph-server` at boot/runtime rather
than exercised by `policy test`. It is still gated in CI: `lint_bundles.py`
structurally checks it every run (group references, action names, scope/action
compatibility, allow-only), so a group-name typo or YAML error — which would
otherwise deny all users `graph_list` at runtime — fails the build.
`tests/server.tests.yaml` records the intended decisions for when upstream adds a
server-scope semantic harness.

### D4 — Maintenance is gated by IAM, not Cedar

`repair`/`optimize`/`cleanup` cannot be Cedar-gated. Access is restricted with
AWS IAM on the backing bucket. There is no `svc-witan-admin` Cedar principal.
(Schema application — `schema_apply` — *is* Cedar-gateable and is owned by
`witan-service`; only the storage-maintenance ops fall to IAM.)

**Amended 2026-08-05 — there IS a `svc-witan-admin` Cedar principal, for a
different class of maintenance.** The original wording conflated two things that
happen to share the word "maintenance":

- **Storage** maintenance (`repair`/`optimize`/`cleanup`) — still IAM-only, still
  correct as written. These commands reject `--server` outright and reach the S3
  store behind the running server's back, so no policy engine is in the path.
  They run as the omnigraph stack's CronJobs
  (ol-infrastructure `applications/omnigraph/maintenance.py`).
- **Data and schema** maintenance (`witan migrate topics` / `migrate repo-keys` /
  `migrate merge`, and a forced `witan migrate schema`) — goes **through the
  server**, so Cedar *is* in the path and default-deny applies. Something has to
  be granted, and the only alternatives to a purpose-made principal were both
  worse: run these as `svc-witan-ci` (the code-graph pipeline, which has no
  business on the memory graph, and which the in-cluster migration Job was in
  fact borrowing) or as `svc-witan-service` (which would have to gain `change` on
  the memory graph, widening the credential the whole serving tier holds).

So `witan-admin` (`act-svc-witan-admin`) is added to all four bundles, with the
narrowest grant the operations need: on the memory graph
`read`/`export`/`invoke_query`/`change` + `schema_apply` (the backfills rewrite
rows in place, and this graph is writable by every human user anyway, so the only
thing it adds over a user actor is schema); on the code and bridge graphs
`read`/`export`/`invoke_query` + `schema_apply` **only** — no `change`,
`branch_merge`, or `branch_delete`, because those graphs are re-derivable, their
promotion into `main` is CI's, and Cedar cannot tell whose WIP view a delete
targets. `witan-ci` remains the only holder of `branch_delete`.

The token is provisioned in ol-infrastructure's omnigraph stack and mounted only
into in-cluster Jobs; no human ever holds it (`witan login` gets an operator
their own `act-<sub>`). See `policy/README.md` § "Non-human actors" and
ol-infrastructure `docs/witan-admin-break-glass-runbook.md`.

### D5 — CI validates and unit-tests the bundle against the real binary

`policy/check.sh` (1) runs `lint_bundles.py` — a structural lint of all four
bundles including the server bundle; (2) converges a fixture cluster
(`policy/cluster.yaml`, stub graphs `memory`/`code_example`/`bridge`) and runs
`omnigraph policy validate`; (3) runs 71 declarative `policy test` cases across
the three per-graph bundles. It runs as the `witan (Cedar policy bundle)` job in
`witan-tests.yml`. The fixture is a test harness; the deployed cluster.yaml is
templated by ol-infrastructure.

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
