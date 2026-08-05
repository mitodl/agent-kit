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
- **No branch-name predicates, and no conditions at all.** A rule compiles to a
  bare `permit(principal in Omnigraph::Group::"…", action == …, resource == …)`
  — the generated Cedar has no `when {}` clause. Combined with the point above,
  that means **ownership is inexpressible**: witan-code names each branch view
  for its writer (`act-<sub>/<branch>`), but no bundle can say "write only views
  prefixed with your own actor id". That rule is enforced client-side instead —
  see `docs/adr/0006-code-graph-branch-ownership-and-reaping.md`, and the
  deliberately-`allow` test case that pins the gap.
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

Four groups. Humans are **per-user** actors — identity is per-user, not
per-team, per `docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md`; the three
non-human actors are single service accounts:

| Group           | Members                                            | Role |
| --------------- | -------------------------------------------------- | ---- |
| `witan-users`   | one `act-<sub>` per authenticated human (Keycloak) | interactive read/write |
| `witan-ci`      | `act-svc-witan-ci`                                  | code-graph **data** pipeline |
| `witan-service` | `act-svc-witan`                                     | the MCP service's own account: **schema** owner |
| `witan-admin`   | `act-svc-witan-admin`                               | **break-glass** in-cluster maintenance (ADR-0005 path b) |

The `act-*` ids committed here are **illustrative fixtures**. In the deployed
cluster, ol-infrastructure templates the real membership from Keycloak claims
(for `witan-users`) and Vault-provisioned service tokens (for the three service
accounts). Keep the group **names** identical across bundle files — only the
membership lists are templated.

### Non-human actors

Three distinct non-human identities, deliberately kept separate:

- **`witan-ci` (`act-svc-witan-ci`) — the code-graph data pipeline.** Owns the
  canonical `main` index for each repo: reindex-on-merge (`change` any branch,
  `branch_merge` into protected `main`) and the WIP-branch lifecycle
  (`branch_create`/`branch_delete` on unprotected branches). It is the **only**
  holder of `branch_delete` — deleting a view Cedar cannot prove you own is the
  stale-view reaper's job (`witan-code reap-views`), not a user's. It **cannot**
  delete protected `main` and **cannot** apply schema. It has **no** access to
  the memory graph.
- **`witan-service` (`act-svc-witan`) — the MCP service's own account.** Schema
  definition and migration is a **default, service-owned operation**: the witan
  MCP service applies the appropriate schema/migrations on every graph
  (`schema_apply`) as part of its normal boot/ownership duties — it is not a
  per-user permission and not the reindex pipeline. It gets `read` to decide
  whether a migration is needed, and `schema_apply`; nothing else. (`omnigraph
  repair`/`optimize`/`cleanup` remain outside Cedar entirely → AWS IAM.)
- **`witan-admin` (`act-svc-witan-admin`) — break-glass maintenance.** The
  principal the in-cluster maintenance Job authenticates as: `witan migrate
  topics` / `migrate repo-keys` / `migrate merge` / a forced `schema` apply, none
  of which are `@mcp.tool` and none of which have a per-user identity to scope
  (`docs/adr/0005-secure-cli-path-into-deployed-witan.md` path b). Its grant is
  **asymmetric by graph, and that asymmetry is the whole design**:
  - On the **memory** graph it gets `read`/`export`/`invoke_query`/`change` +
    `schema_apply`. `change` is unavoidable — the backfills rewrite existing rows
    in place, and Cedar's finest scope is graph + branch, so "only rows nobody
    else owns" cannot be expressed. It costs nothing: this graph is team-shared
    and every `witan-users` member can already write all of it, so the only thing
    the admin adds over a human is `schema_apply`.
  - On the **code** and **bridge** graphs it gets `read`/`export`/`invoke_query` +
    `schema_apply` and **nothing else**. No `change` (a code graph is
    re-derivable — the fix for a bad index is a reindex, by CI or the view's own
    writer), no `branch_merge` (promotion into the reviewed `main` is a
    deliberate CI act with a git merge behind it), no `branch_delete` (Cedar
    cannot tell whose view a delete targets, so the grant would make one token a
    way to destroy every developer's in-flight index).
  - Never held by a human. An operator working interactively authenticates with
    `witan login` as their own `act-<sub>` (ADR-0005 path a); the admin token
    lives only in the cluster, mounted into Jobs.

## The bundles

| File                     | applies_to        | Grants                                                                                       |
| ------------------------ | ----------------- | -------------------------------------------------------------------------------------------- |
| `memory.policy.yaml`     | `[memory]`        | users: read/export/invoke + change on the flat shared work graph. service: read/export + schema_apply. admin: read/export/invoke/change + schema_apply. (no CI)  |
| `code-graph.policy.yaml` | per-repo graph ids | users: read/export/invoke anywhere, change + `branch_create` on **unprotected** (WIP) branches only. CI: read/export/change + merge into protected `main` + WIP branch lifecycle incl. `branch_delete`. service: read/export + schema_apply. admin: read/export/invoke + schema_apply only. |
| `bridge.policy.yaml`     | `[bridge]`        | users: read/export/invoke anywhere, change + `branch_create` on unprotected WIP views. CI: read/export/change any + WIP branch lifecycle. service: read/export + schema_apply. admin: read/export/invoke + schema_apply only. |
| `server.policy.yaml`     | `[cluster]`       | graph_list, to all four groups. *(Not in the CI harness — see below.)*                        |

### Layer topology → graph mapping

- **Layer 1 — `memory`**: the team-shared work/knowledge graph (Memory, Task,
  Workflow\*, CodeBranch). Flat, single-branch: everyone reads and writes it.
- **Layer 2 — per-repo code graphs**: one graph per indexed repo, id
  `sanitize_slug(repo)`. Branch model (witan-code `repo.py:store_branch`): the
  repo's **default git branch → store `main`** (the committed, reviewed index,
  marked `protected`); every other git branch → a non-`main` store branch
  (`unprotected`, per-user/per-session WIP), named for its writer as
  `act-<sub>/<branch>`. Humans reindex freely on WIP branches but never mutate
  `main`; `svc-witan-ci` owns main-branch writes and merges, so promotion into
  `main` is deliberate. To Cedar a namespaced view is just an unprotected
  branch name — the `act-<sub>/` prefix is enforced by witan-code, not here.
- **Layer 2.5 — `bridge`**: the cross-repo bridge (`_bridge.omni`), a derived
  projection. Derived, but neither flat nor read-only for humans: indexing a WIP
  git branch writes that branch's bindings here too, on a repo-qualified view of
  its own (`act-<sub>/<repo-slug>/<branch>`). `main` is the committed,
  CI-written projection.

## Validate & test

```bash
# once: install the pinned omnigraph binary the way CI does
uv run python -c "from witan_core.omnigraph_install import install_omnigraph; install_omnigraph(dry_run=False)"
export PATH="$HOME/.local/bin:$PATH"

./policy/check.sh          # lint all 4 bundles, validate 3, run 71 test cases
```

`check.sh` does three things: (1) `lint_bundles.py` — a structural lint of
**every** bundle including `server.policy.yaml` (group references resolve,
actions are known, scopes match their actions, allow-only); (2) `policy
validate` on the three per-graph bundles; (3) `policy test` — 71 declarative
allow/deny cases. It runs in CI as the `witan (Cedar policy bundle)` job in
`.github/workflows/witan-tests.yml`. `cluster.yaml` here is a **CI test
harness**, not the deployed config — it wires the bundles onto three stub graphs
(`memory`, `code_example`, `bridge`) so `policy validate`/`policy test` have
concrete graph ids without real stores. `tests/*.tests.yaml` are declarative
allow/deny assertions.

## Server-level bundle (`server.policy.yaml`)

`graph_list` is server-scoped (bound to `Omnigraph::Server::"root"`).

**Applying this bundle does not by itself make `graphs list` usable.** Two
independent gates, both measured against 0.8.1 (2026-08-05):

1. Without any bundle, the server refuses outright — "server-scoped actions
   require an explicit cluster policy bundle applied with `omnigraph cluster
   apply` … the management surface is closed by default in every runtime state,
   including `--unauthenticated`". This bundle fixes that one.
2. *With* the bundle and a granted token, the server answers but the **CLI**
   then refuses to print it: `server scope '<url>' has N graphs: pass --graph
   <id> to select one`. `graphs list` has no working invocation against a
   multi-graph server (`--uri` is retired: "a remote graph must be addressed
   with `--server <url>`"). Upstream bug; nothing here fixes it.

Consequence for clients: **nothing on a write path may depend on enumeration.**
witan-code asks the graph-scoped question instead — `branch list --graph <id>`,
needing only `read` — see `witan_code.store.probe_cluster_graph`. Depending on
`graphs list` is what left every environment's code-graph indexer failing before
it parsed a single file.

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

**Applying any bundle is a hard cutover to authenticated-everything.** Measured
against omnigraph 0.8.1 (2026-08-05): a `policies:` block in cluster.yaml makes
`omnigraph-server` *refuse to boot* without bearer tokens —

> policy file is configured but no bearer tokens — every request would 401
> because no token can ever match.

— and once it does boot, an actor with no token 401s and an actor with a token
but no grant gets `policy denied action '…' for unknown actor '…'`. So the
token map has to be complete and mounted **before** the first bundle is applied,
not alongside it. Deploy order per environment: provision every actor's token →
verify the map → apply the bundle → restart. There is no partial mode.

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
5. **Schedule the stale-view reaper.** `witan-ci`'s `branch_delete` grant is
   authorization for a job that has to actually run: every developer's every git
   branch gets a view on the shared graph, and nothing else removes one. Run
   `witan-code reap-views --store <server-url> --graph <id> --apply` with
   `WITAN_CODE_INDEX_ROLE=ci` and the `svc-witan-ci` token, per graph, on a
   schedule (`WITAN_CODE_VIEW_MAX_IDLE_DAYS`, default 14). See
   `docs/adr/0006-code-graph-branch-ownership-and-reaping.md` D5.
