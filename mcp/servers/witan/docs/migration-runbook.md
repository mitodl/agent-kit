# Store migration runbook: local → shared, and cross-machine merges

How to move a witan store onto the shared deployment
([ADR-0009](https://github.com/mitodl/ol-infrastructure/blob/main/docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md)),
between two of your own machines, or merged with a teammate's. All three are
`witan migrate merge` with a different destination.

> **Never `mv`, `cp`, rsync, or tar a `.omni` store.** Lance embeds absolute
> paths, so a copied store fails to open or reads the wrong files. Move data
> with `witan migrate merge` (or an `omnigraph export` file), never with the
> filesystem.

## Local → shared: the cutover

Moving your own store onto the deployment. No kubectl, no port-forward, no AWS
credentials.

**1. Register the deployment and log in** (once — hostnames are in
[`deployed-witan-onboarding.md`](deployed-witan-onboarding.md)):

```bash
witan target add ol --remote-url … --oidc-issuer …
witan login --target ol
```

**2. Preview the merge:**

```bash
witan migrate merge ~/.local/share/witan/graph.omni --to ol --dry-run
```

`--to ol` names the target block, so the destination is on the command line
rather than in your environment. Read the decisions: `added` should be roughly
the row count of your store, and `updated` should be small. A large `updated`
on a first migration means slugs are colliding that shouldn't — stop there.

**3. Run it:**

```bash
witan migrate merge ~/.local/share/witan/graph.omni --to ol
```

Your store is exported locally and the rows ship through the deployment's
`store_merge` tool in batches, written **as you**, under your own credential.
Batches commit independently, so a failure part-way leaves earlier batches
applied — just re-run, the merge is idempotent.

**4. Verify by slug, not by search:**

```bash
witan whoami
witan memory --kind lesson --all-repos | head    # a listing, not a search
witan task <a-task-slug-you-recognise>
witan tasks --all-repos | head
```

`witan memory "<some words>"` will very likely return *No memories.* on a
freshly-populated graph even when every row is present — that is a BM25
property of a small corpus, not a failed merge
([why](store-merge-findings.md#search-looks-broken-on-a-near-empty-graph--verify-by-slug)).
Listings and `witan task <slug>` read the graph directly and do not go through
BM25, which is what makes them usable here.

**5. Keep your local store until you have verified.** It is the backup.

Once everyone has merged, an operator runs `witan migrate repo-keys` and
`witan migrate topics` **once**, in-cluster (see the fallback below).

### Or hand the cutover to an agent

Same steps, run by Claude/pi instead of by you. Paste this, replacing `ol`
with your target name if it differs:

```text
Run my witan local-to-shared cutover, following
mcp/servers/witan/docs/migration-runbook.md's "Local → shared: the cutover".

1. `witan whoami --target ol`. If it says I am not logged in, stop and tell me
   to run `witan login --target ol` — do not attempt the login yourself.
2. `witan migrate merge ~/.local/share/witan/graph.omni --to ol --dry-run`.
   Report the added/updated/kept counts. STOP AND ASK before going further if
   `updated` is more than a handful — on a first migration that means slugs are
   colliding that should not, and each one silently drops a record.
3. Once I approve, run the same command without --dry-run.
4. Verify with `witan memory --kind <kind> --all-repos` listings and
   `witan task <slug>` on two or three slugs from the dry-run decision list.
   Do NOT verify with `witan memory "<words>"` — that is a BM25 search, and it
   returns nothing on a small corpus even when every row landed, so an empty
   result is not evidence of anything.
5. Report what landed. Do not delete, move, or clean up my local store — it is
   the backup until I say otherwise.

If any step fails, stop and show me the error rather than retrying or working
around it.
```

The guardrails are the point: the merge is idempotent, so re-running is safe,
but a large `updated` count and a "search finds nothing" reading are both
things an agent will otherwise sail past.

## Cross-machine merge and machine migration

Same command, a store or a named target on each end:

```bash
# two named local targets — neither end is a path anyone types
witan migrate merge --from personal --to work

# by path: merge each machine's store into a shared third one
witan migrate merge machine-a.omni --target combined.omni
witan migrate merge machine-b.omni --target combined.omni

# machine migration: the target starts empty and is created automatically
witan migrate merge old-machine.omni --target new-machine.omni

# from a store that can't travel — hand over its export instead
omnigraph export --store ~/.local/share/witan/graph.omni > alice.jsonl
witan migrate merge alice.jsonl --target combined.omni
```

Preview with `--dry-run` first, then verify:

```bash
omnigraph export --store <target> | jq -r .type | sort | uniq -c
```

Type counts in the target should equal the union of the sources' counts, minus
collisions resolved in the target's favour.

## Flags

```
witan migrate merge [SOURCE] [--from <name>] [--to <name>] [--target <uri>] [--dry-run]
```

| Flag | Means |
|---|---|
| `SOURCE` | Store URI to merge **from**: local path, `s3://`, `file://`, `http(s)://`, or a local `omnigraph export` `.jsonl`. |
| `--from <name>` | A `[targets.<name>]` block's `server`, in place of `SOURCE`. A target with only a `remote_url` is refused — nothing local to export. |
| `--to <name>` | A `[targets.<name>]` block as the destination: through its deployment if it has a `remote_url`, into its `server` store if not. |
| `--target <uri>` | A destination store URI. Defaults to your configured store. Mutually exclusive with `--to`; `.jsonl` is refused (a target is a graph, not a snapshot). |
| `--dry-run` | Print the per-slug decisions, write nothing. |

Notes:

- A `.jsonl` **source** must be a readable local file — witan fetches no remote
  exports. Download it first (`aws s3 cp …`) and pass the path.
- A missing local destination is created and schema-applied; a missing remote
  one is assumed to exist.
- A deployed graph addressed by URI is
  `http(s)://<host>:<port>/graphs/<graph-id>` — the `/graphs/<id>` part is
  required.
- Against a deployment, `--target` is refused: the destination is that
  deployment's own graph, resolved server-side. Use `--to <name>`.
- Merging is **repeatable**. A re-run against an already-merged target loads
  nothing, because every source row loses reconciliation to its own applied
  copy. Safe on a schedule.
- Reconciliation covers nodes only. Edge rows (`Tagged`, `ParentOf`, …) have no
  slug and pass through unreconciled, same as raw `--mode merge`.

## Fallback: in-cluster merge (operator)

Use when the MCP tier is unavailable, or to merge on someone else's behalf.
Every write lands as `svc-witan-admin` rather than as the user, which is why
this is the fallback. The data tier is ClusterIP-only, so the merge runs inside
the cluster; the user's store cannot travel, so they hand over its export.

**1. User exports and stops writing locally:**

```bash
omnigraph export --store ~/.local/share/witan/graph.omni > "$USER-witan.jsonl"
wc -l "$USER-witan.jsonl"     # thousands of rows, not zero
```

**2. Operator opens a break-glass pod and streams the file in:**

```bash
kubectl -n witan create job witan-bg-$(date +%s) --from=cronjob/witan-break-glass
JOB=witan-bg-<id>      # from the output above

kubectl -n witan exec -i job/$JOB -- sh -c 'cat > /tmp/alice.jsonl' < alice-witan.jsonl
kubectl -n witan exec -it job/$JOB -- wc -l /tmp/alice.jsonl
```

`kubectl exec -i`, not `kubectl cp` and not S3: the pod declares no volume and
no ServiceAccount, so it holds no bucket credentials and no `aws` binary. Check
an unusually large export against the pod's 1Gi memory limit.

**3. Operator dry-runs, reviews, merges.** The pod's `WITAN_MEMORY_URI` and
`WITAN_MEMORY_TOKEN` already address the in-cluster graph, so no destination
flag is needed:

```bash
kubectl -n witan exec -it job/$JOB -- witan migrate merge /tmp/alice.jsonl --dry-run
kubectl -n witan exec -it job/$JOB -- witan migrate merge /tmp/alice.jsonl
```

**4. Repeat steps 1–3 per user**, then run the post-merge migrations once at
the end and clean up:

```bash
kubectl -n witan exec -it job/$JOB -- witan migrate repo-keys
kubectl -n witan exec -it job/$JOB -- witan migrate topics
kubectl -n witan delete job $JOB
```

**5. User verifies** through the deployment, as in step 4 of the cutover above.

## Fallback: no `witan` CLI, only `omnigraph`

```bash
omnigraph export --store <source> > data.jsonl
omnigraph init --schema schema/schema.pg <target>    # skip if the target exists
omnigraph load --store <target> --data data.jsonl --mode merge --as <actor>
```

Run the `load` once per source when merging several. This has **no**
reconciliation: a slug present in both stores is silently overwritten by
whichever file loads last. Diff the slug sets first and resolve any hit by hand:

```bash
omnigraph export --store <target> | jq -r 'select(.data.slug) | .data.slug' | sort > target-slugs.txt
jq -r 'select(.data.slug) | .data.slug' machine-a.jsonl | sort | comm -12 target-slugs.txt -
```

A `Topic` hit is harmless (deterministic, content-equivalent by design). A
`Memory`/`Task`/`WorkflowProject`/`WorkflowSession` hit is two different
records, one about to disappear.

## Why it works this way

[`store-merge-findings.md`](store-merge-findings.md) — the verified `--mode
merge` collision behaviour, the slug-collision probability, and the BM25
measurement behind "verify by slug, not by search".

## References

- [`deployed-witan-onboarding.md`](deployed-witan-onboarding.md) — the other
  half of the cutover: pointing your CLI and agent at the deployment.
- [ADR-0007](adr/0007-local-to-shared-store-migration-transport.md) — why the
  local → shared path is a client-side export merged through the MCP tier.
- ol-infrastructure `docs/witan-admin-break-glass-runbook.md` — the
  `witan-break-glass` pod and its `svc-witan-admin` token.
