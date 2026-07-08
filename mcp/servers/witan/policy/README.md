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

Two groups, expressed as collections of **per-user** actors (per
`tk-design-keycloak-jwt-omnigraph-per-user-actor-tok-728f0c` — identity is
per-user, not per-team):

| Group         | Members                                            |
| ------------- | -------------------------------------------------- |
| `witan-users` | one `act-<sub>` per authenticated human (Keycloak) |
| `witan-ci`    | `act-svc-witan-ci` — the one non-human/shared actor |

The `act-*` ids committed here are **illustrative fixtures**. In the deployed
cluster, ol-infrastructure templates the real membership from Keycloak claims.
Keep the group **names** identical across all four bundle files — only the
membership lists are templated.

## The bundles

| File                     | applies_to        | Grants                                                                                       |
| ------------------------ | ----------------- | -------------------------------------------------------------------------------------------- |
| `memory.policy.yaml`     | `[memory]`        | users: read/export/change/invoke on the flat shared work graph. CI: schema_apply.            |
| `code-graph.policy.yaml` | per-repo graph ids | users: read/invoke anywhere, change + branch ops on **unprotected** (WIP) branches only. CI: change/merge/schema_apply on protected `main`. |
| `bridge.policy.yaml`     | `[bridge]`        | users: read/export/invoke. CI: change/schema_apply (the bridge is CI-derived).               |
| `server.policy.yaml`     | `[cluster]`       | users + CI: graph_list. *(Not in the CI harness — see below.)*                               |

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

./policy/check.sh          # converge fixture, validate 3 bundles, run 24 test cases
```

`check.sh` runs in CI as the `witan (Cedar policy bundle)` job in
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
enforced by `omnigraph-server` at boot/runtime) but excluded from `check.sh`.
`tests/server.tests.yaml` documents the intended decisions for when a
server-scope validation path lands upstream.

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
