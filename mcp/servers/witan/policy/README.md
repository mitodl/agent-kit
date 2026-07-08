# witan v1 Cedar policy bundle

Authorization for the **shared, multi-user, deployed** witan omnigraph store.
This directory holds the policy bundle that `omnigraph-server` enforces, plus a
CI harness that validates and unit-tests it against the real `omnigraph` binary.

Tracked by `tk-witan-v1-cedar-policy-bundle-team-repo-scoped-re-77655e`
(project `wp-witan-multi-user-service-deployment-dcf6ee`). See also
ol-infrastructure `docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md`
and this repo's `docs/adr/0002-witan-cedar-authorization-bundle.md`.

## The omnigraph authorization model (what's expressible)

From omnigraph `docs/user/operations/policy.md` and the `omnigraph-policy`
crate — the constraints that shaped this bundle:

- **Allow-only.** Every rule is a `permit`. There is no `deny` key. Anything
  ungranted is denied by default. You restrict an action by *not granting* it.
- **Actor identity is server-resolved** from the matched bearer token — never
  from a client-supplied header, query param, or body field.
- **Finest scope is graph + branch.** There is **no per-node-type or per-row**
  authority — "Per-type and per-row authority is the query-layer's job" and is
  not implemented server-side. So within the Layer-1 graph we cannot say
  "Memory yes, Task no"; anyone who can `change` the graph can change every
  node kind in it.
- **Actions.** Per-graph: `read`, `export`, `change`, `schema_apply`,
  `branch_create`, `branch_delete`, `branch_merge`, `invoke_query`, `admin`.
  Server-scoped: `graph_list`.
- **Branch scoping.** `read`/`export`/`change` take `branch_scope` (the source
  branch); `schema_apply`/`branch_*` take `target_branch_scope` (the
  destination). Values: `any | protected | unprotected`. `protected_branches:`
  is a bundle-level list of branch names. `invoke_query` and `graph_list` are
  unscoped.
- **`omnigraph repair`/`optimize`/`cleanup` are direct-storage-only** and cannot
  be Cedar-gated at all. Restrict maintenance with **AWS IAM on the bucket**,
  not a Cedar principal.

## Actors and groups

Three groups. Humans are **per-user** actors (per
`docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md` — identity is
per-user, not per-team); the two non-human actors are single service accounts:

| Group           | Members                                            | Role |
| --------------- | -------------------------------------------------- | ---- |
| `witan-users`   | one `act-<sub>` per authenticated human (Keycloak) | interactive read/write |
| `witan-ci`      | `act-svc-witan-ci`                                  | code-graph **data** pipeline |
| `witan-service` | `act-svc-witan`                                     | the MCP service's own account: **schema** owner |

The `act-*` ids committed here are **illustrative fixtures**. In the deployed
cluster, ol-infrastructure templates the real membership from Keycloak claims
(for `witan-users`) and Vault-provisioned service tokens (for the two service
accounts). Keep the group **names** identical across bundle files — only the
membership lists are templated.

### Non-human actors

Two distinct non-human identities, deliberately kept separate:

- **`witan-ci` (`act-svc-witan-ci`) — the code-graph data pipeline.** Owns the
  canonical `main` index for each repo: reindex-on-merge (`change` any branch,
  `branch_merge` into protected `main`) and the WIP-branch lifecycle
  (`branch_create`/`branch_delete` on unprotected branches — the stale-branch
  reaper). It **cannot** delete protected `main` and **cannot** apply schema. It
  has **no** access to the memory graph. Its enforcement is in place ahead of
  the pipeline itself, which is a separate task
  (`tk-branch-cedar-gating-stale-code-graph-branch-reap-0c621c`).
- **`witan-service` (`act-svc-witan`) — the MCP service's own account.** Schema
  definition and migration is a **default, service-owned operation**: the witan
  MCP service applies the appropriate schema/migrations on every graph
  (`schema_apply`) as part of its normal boot/ownership duties — it is not a
  per-user permission and not the reindex pipeline. It gets `read` to decide
  whether a migration is needed, and `schema_apply`; nothing else. (`omnigraph
  repair`/`optimize`/`cleanup` remain outside Cedar entirely → AWS IAM.)

## The bundles

| File                     | applies_to        | Grants                                                                                       |
| ------------------------ | ----------------- | -------------------------------------------------------------------------------------------- |
| `memory.policy.yaml`     | `[memory]`        | users: read/export/change/invoke on the flat shared work graph. service: read + schema_apply. (no CI)  |
| `code-graph.policy.yaml` | per-repo graph ids | users: read/invoke anywhere, change + branch ops on **unprotected** (WIP) branches only. CI: read/change + merge into protected `main` + WIP branch lifecycle. service: read + schema_apply. |
| `bridge.policy.yaml`     | `[bridge]`        | users: read/export/invoke. CI: read + change (bridge content). service: schema_apply.        |
| `server.policy.yaml`     | `[cluster]`       | users: graph_list. *(Not in the CI harness — see below.)*                                    |

### Layer topology → graph mapping

- **Layer 1 — `memory`**: the team-shared work/knowledge graph (Memory, Task,
  Workflow\*, CodeBranch). Flat, single-branch: everyone reads and writes it.
- **Layer 2 — per-repo code graphs**: one graph per indexed repo, id
  `sanitize_slug(repo)`. Branch model (witan-code `repo.py:store_branch`): the
  repo's **default git branch → store `main`** (the committed, reviewed index,
  marked `protected`); every other git branch → a non-`main` store branch
  (`unprotected`, per-user/per-session WIP). Humans reindex freely on WIP
  branches but never mutate `main`; `svc-witan-ci` owns main-branch writes,
  merges, and schema applies, so promotion into `main` is deliberate.
- **Layer 2.5 — `bridge`**: the cross-repo bridge (`_bridge.omni`), a derived
  projection — read-only for humans, CI-written.

## Validate & test

```bash
# once: install the pinned omnigraph binary the way CI does
uv run python -c "from witan.setup import install_omnigraph; install_omnigraph(dry_run=False)"
export PATH="$HOME/.local/bin:$PATH"

./policy/check.sh          # lint all 4 bundles, validate 3, run 38 test cases
```

`check.sh` does three things: (1) `lint_bundles.py` — a structural lint of
**every** bundle including `server.policy.yaml` (group references resolve,
actions are known, scopes match their actions, allow-only); (2) `policy
validate` on the three per-graph bundles; (3) `policy test` — 38 declarative
allow/deny cases. It runs in CI as the `witan (Cedar policy bundle)` job in
`.github/workflows/witan-tests.yml`. `cluster.yaml` here is a **CI test
harness**, not the deployed config — it wires the bundles onto three stub graphs
(`memory`, `code_example`, `bridge`) so `policy validate`/`policy test` have
concrete graph ids without real stores. `tests/*.tests.yaml` are declarative
allow/deny assertions.

## Server-level bundle (`server.policy.yaml`)

`graph_list` is server-scoped (bound to `Omnigraph::Server::"root"`).
omnigraph 0.8.1's offline `policy validate`/`policy test` load every applied
bundle under the **per-graph** engine, which (a) rejects `graph_list` in a graph
bundle and (b) fans a `[cluster]`-scoped bundle onto every graph, tripping the
"one bundle per graph scope" selector. There is no offline CLI path to validate
a server-scoped bundle in this version. `server.policy.yaml` is therefore kept
as a deploy-time artifact (applied by ol-infrastructure's cluster.yaml,
enforced by `omnigraph-server` at boot/runtime) and excluded from `policy
validate`/`policy test`. It is **not** unguarded, though: `lint_bundles.py`
structurally checks it on every run — catching group-name typos, unknown
actions, and YAML errors that would otherwise deny all users `graph_list` at
runtime. `tests/server.tests.yaml` documents the intended allow/deny decisions
for when a server-scope semantic-validation path lands upstream.

## Deploying (ol-infrastructure)

The deployed `cluster.yaml` (templated by ol-infrastructure, **not** the fixture
here) must:

1. Enumerate the real graphs: `memory`, `bridge`, and one
   `sanitize_slug(repo)` per indexed repo.
2. Wire the four bundles via `policies:` + `applies_to`, listing **every**
   real code-graph id in `code-graph.policy.yaml`'s `applies_to`.
3. Template each bundle's `groups:` membership from Keycloak claims
   (`witan-users` = per-user actors; `witan-ci` = `act-svc-witan-ci`).
4. Gate maintenance ops (`repair`/`optimize`/`cleanup`) with **AWS IAM** on the
   backing bucket — Cedar cannot.
