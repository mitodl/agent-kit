<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan/docs/adr/0007-local-to-shared-store-migration-transport.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan/docs/adr/0007-local-to-shared-store-migration-transport.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/adr/0007-local-to-shared-store-migration-transport.md).

# 7. Transport for a local → shared store migration

- Status: Accepted
- Date: 2026-08-06
- Deciders: witan platform owners
- Tracking: task `tk-no-usable-transport-for-a-local-shared-store-mig-afbf18`,
  task `tk-remote-server-registration-local-remote-user-dat-dc753c`,
  task `tk-un-defer-adr-0007-d5-merge-through-the-witan-mcp-f1e5a1` (D5),
  project `wp-witan-multi-user-service-deployment-dcf6ee`
- Supersedes: —
- Amends: `0005-secure-cli-path-into-deployed-witan.md` (path (b) gains a
  concrete data-movement procedure; the ADR provisioned the principal but never
  said how bytes reach it)
- Related: `0002-witan-cedar-authorization-bundle.md`;
  `0004-keycloak-jwt-per-user-actor-mapping.md`; ol-infrastructure
  `docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md` (the
  ClusterIP-only data tier this works within) and
  `docs/witan-admin-break-glass-runbook.md`; `docs/migration-runbook.md`

## Context

The migration *procedure* was built and tested; the *transport* to the deployed
graph was not. `witan migrate merge <source> --target-uri <target>` exports both
sides, reconciles node collisions on `updated_at` rather than last-write-wins,
and is idempotent — the hard part, and it works. But it addressed every store
as `--store <uri>`, and none of the reachable spellings of "the deployed graph"
survive that:

1. **`--target s3://ol-data-witan-<env>`** — the bucket's only IAM grant is the
   `omnigraph-server` ServiceAccount's IRSA role, in the `omnigraph` namespace
   (ol-infrastructure `applications/omnigraph/data_tier.py`). No human IAM path
   to it is declared anywhere, and minting one would route writes around the
   bearer-token/Cedar model ADR-0009 exists to establish.
2. **`--target http://omnigraph-server.omnigraph.svc.cluster.local:8080`** —
   ClusterIP only, never exposed (ADR-0009 decision point 2). *And*, even from
   inside the cluster, this failed: omnigraph 0.8.1 rejects an http(s)
   `--store` outright. A remote graph is addressable only as
   `--server <url> --graph <id>`.
3. **Through the public MCP endpoint** (`witan.ol.mit.edu`) — `merge_store` is
   in `_ADMIN_ONLY` and is not an `@mcp.tool` at all, deliberately: a bulk
   store merge has no per-user identity to scope (ADR-0005 path b).

Point 2 is the one that mattered most, because it was not a policy limit but a
plain defect. The in-cluster maintenance pods that *do* have the right network
position and credential — `witan-break-glass` and the pre-deploy migration Job
— are configured with `WITAN_MEMORY_URI` pointed at the ClusterIP server
(ol-infrastructure `applications/witan/break_glass.py`). So the sanctioned
break-glass path already ran with a remote-addressed store, and
`witan migrate merge` was the one command in the image that could not use it.
`OmnigraphClient._store_args()` had encoded the correct rule since the 0.8.1
upgrade; `merge_store` simply bypassed it.

The second gap is the one nobody had written down: **a store cannot travel.**
Lance embeds absolute paths, so a user's `~/.local/share/witan/graph.omni`
cannot be copied to another machine, streamed into a pod, or staged in a
bucket. Only its `omnigraph export` output can. The break-glass runbook
acknowledged this and told operators to "copy it in or export/load it through
S3 first" — but the break-glass pod declares no volume and no ServiceAccount,
so it holds neither S3 credentials nor an `aws` binary. The advice was not
executable, and `witan migrate merge` had no way to consume an export file
even once one arrived.

## Decision

**Make the existing in-cluster path work, rather than building a new one.**
Two changes to `merge_store`, no new infrastructure, no new server surface:

### D1 — Address each store the way the omnigraph CLI requires

`merge_store` resolves the source and the target independently through
`witan_core.omnigraph.store_cli_args()` / `store_subprocess_env()` — the
free-function forms of the client's own `_store_args`/`_subprocess_env`, which
now delegate to them so there is one implementation of the rule. A local path
or `s3://` root stays `--store <uri>`; an `http(s)://` store becomes
`--server <url> --graph <id>`, with the graph id taken from the configured
`WITAN_MEMORY_GRAPH` or written inline as `http://host:8080/graphs/<id>`.

The bearer token travels with it: a remote store gets the configured token when
the URI names the configured store (the in-cluster case, where
`WITAN_MEMORY_TOKEN` is the pod's only credential) and otherwise inherits the
ambient `OMNIGRAPH_BEARER_TOKEN`. A local store has an ambient token *stripped*
rather than merely unset — it has no server to present one to, and a token
exported for cluster use should not ride into an unrelated subprocess.

### D2 — Accept an `omnigraph export` JSONL as the merge source

Any `source` ending `.jsonl` is read as an export rather than re-exported. The
suffix is unambiguous — an omnigraph store is a Lance *directory*, never a file
— so this needs no flag. This is what makes the export, the only transportable
form of a store, a first-class input to the merge.

It also makes the established file-ingress idiom sufficient. There is no
volume, PVC, or bucket path into the maintenance pods, but there is
`kubectl exec -i`, already proven for exactly this in ol-infrastructure's
storage-format upgrade runbook:

```bash
kubectl -n witan exec -i job/witan-bg-<id> -- sh -c 'cat > /tmp/alice.jsonl' < alice.jsonl
```

### D3 — The resulting supported route

A user exports locally and hands the file over; an operator streams it into a
break-glass pod and merges. Full procedure in `docs/migration-runbook.md`
§ "Local → shared". Every write lands as `svc-witan-admin`, which is correct:
a bulk store merge is an administrative act, and witan's own `author` field on
each row preserves who actually wrote it.

For a user with cluster credentials, the same two commands work over a
`kubectl port-forward` to the data tier, with a `--target
http://127.0.0.1:8080/graphs/council`. That is a convenience, not a second
supported path — it needs the actor's own bearer token out of the
`actor-tokens` Secret, which most users cannot read.

### D4 — Rejected: exposing the data tier

Putting omnigraph-server behind an authenticated ingress so `--server` works
from a laptop would make this fully self-service. It reverses ADR-0009's
explicit "the data tier is never exposed" and adds a second,
policy-unmediated boundary next to the MCP tier — the same reasoning that
rejected it for witan-code's writes in ADR-0005 (c). Listed here so it is
rejected deliberately rather than forgotten.

### D5 — The default path: merge through the MCP tier

**Implemented 2026-08-06, in this change.** The self-service shape mirrors
ADR-0005 (c): mediated store operations through the MCP tier, authorized
per-actor server-side, exactly as `code_store_load` does for the code graph.

The argument is not convenience. Under D1–D3 every merge lands in the omnigraph
audit trail as `svc-witan-admin`; routing through the MCP tier is what puts
Cedar and the per-request actor in the path, which is the premise of the shared
deployment. It also removes the operator from a step a user should be able to
do alone, and makes "safe to run on a schedule" actionable — under D1–D3 a
scheduled merge has nobody to run it as except the admin principal.

The split of work is what keeps this small:

- **`store_merge(rows, dry_run)`** — a real `@mcp.tool`, so every store call in
  it goes through the module-level `client`, which re-resolves to *this
  request's* actor on each access (`_ActorScopedClient` → `_resolve_client`,
  ADR-0004). There is no service account behind it. The server reconciles the
  batch against the graph it already holds a client on, and writes the winners.
- **`RemoteServerProxy.merge_store`** — an explicit method, not a
  `__getattr__` dispatch, because this is not one tool call: the source is
  exported *client-side* (the deployment shares no filesystem with the caller)
  and shipped in `chunk_records` batches. The CLI call site is unchanged, so
  `witan migrate merge` reads identically in both modes.
- **One reconciliation rule.** Both transports call `_reconcile_nodes`; a row's
  fate must not depend on which one carried it.

The cost quoted before this was built was wrong in a useful way. "A reconciling
client (the merge must *export the target* too, so `load` alone is not enough)"
is true of a bare `load` tool, not of a merge-shaped one: the deployed witan
already holds an `OmnigraphClient` on the target, so the **server** does both
halves and only source rows cross the wire. Batching was the real work, and it
was reused rather than reinvented — `chunk_records` moved from witan-code to
`witan_core.chunking` (same 413 ceiling, same node-before-edge rule), and
`load_batch` moved to the base `OmnigraphClient`.

`merge_store` therefore leaves `_ADMIN_ONLY`. The in-process function of the
same name stays for D1–D3 and is still not a tool: two transports for one
operation, which is why they share a name and a call site.

**What this does not do.** `--target` is refused over a deployment — the target
is that deployment's own graph, resolved server-side, since a client never
names a store address (ADR-0005 c). And batches commit independently, so a
failure part-way leaves earlier batches applied; that is recoverable by
re-running rather than atomic, because reconciliation makes a re-sent row lose
to its own already-applied copy.

## Consequences

- **`witan migrate merge` reaches a deployed graph.** The in-cluster
  break-glass path (ADR-0005 b) is now executable end to end, which it was not
  before, and the `witan-break-glass` pod needs no redefinition to support it.
- **The runbook's headline example changed.** `--target s3://witan-shared/…`
  described a target nobody outside the cluster can write to; it is replaced
  with the export → `kubectl exec -i` → merge procedure.
- **`store_cli_args`/`store_subprocess_env` are public witan-core API.** The
  addressing rule was previously private to `OmnigraphClient` and re-implemented
  inline in six places; the two that mattered now share one function. The
  remaining inline `("http://", "https://", "s3://")` checks are a *different*
  predicate (is this store lockable / is it a local path) and were left alone —
  note that `OmnigraphClient.is_remote` treats `s3://` as **not** remote while
  `maintenance.REMOTE_PREFIXES` treats it as remote, and both are correct for
  their own question.
- **Attribution is per-actor on the D5 path, admin-level on the D1–D3 one.**
  Through the MCP tier the omnigraph audit trail records the merge as the
  calling user's `act-<sub>`, and Cedar evaluates it as them. The in-cluster
  path still records `svc-witan-admin` — correct for a bulk administrative
  act, and the reason D1–D3 is now the fallback rather than the default.
- **`witan_core.chunking` and `OmnigraphClient.load_batch` are shared API.**
  Both moved up from witan-code, which now imports them: the merge hits the
  same buffered-body ceiling an index does, so the split rule and the
  node-before-edge ordering live in one place rather than two.
- **No change to the default path.** With a local store configured, every
  command behaves exactly as before — the addressing helper returns the same
  `--store <uri>` it always did.
