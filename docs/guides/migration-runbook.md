<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan/docs/migration-runbook.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan/docs/migration-runbook.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/migration-runbook.md).

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
[`deployed-witan-onboarding.md`](deployed-witan.md)):

```bash
witan target add ol --remote-url … --oidc-issuer …
witan login --target ol
```

**2. Take stock of what is in the store.** The shared graph is org-wide, and a
local store accumulates whatever you worked on — personal repos included. Merge
is all-or-nothing, so decide what goes *before* the dry run:

```bash
omnigraph export --store ~/.local/share/witan/graph.omni > witan-export.jsonl
jq -r 'select(.type) | .data.repo // (.data.repos // [] | join(", ")) // ""
       | if . == "" then "(no repo)" else . end' witan-export.jsonl |
  sort | uniq -c | sort -rn
```

That is every repo represented, by row count. `(no repo)` is mostly general
engineering lessons that belong to no checkout — usually the ones most worth
sharing, so don't drop them by reflex.

If anything on that list should not go, merge a filtered export instead of the
store. Both passes below are needed: `from`/`to` on an edge are slugs, so
dropping a node without dropping its edges leaves the edges dangling.

```bash
# 1. the slugs to leave behind — adjust the predicate to your own list
jq -r 'select(.type)
       | select([.data.repo // empty, (.data.repos // [])[]]
                | any(startswith("https://github.com/alice/")))
       | .data.slug' witan-export.jsonl | sort -u > drop-slugs.txt

# 2. drop those nodes and every edge that touches one
jq -c --rawfile drop drop-slugs.txt '
  ($drop | split("\n") | map(select(length > 0)) | INDEX(.)) as $d
  | select(if .type then ($d[.data.slug] | not)
           else ($d[.from] | not) and ($d[.to] | not) end)
  ' witan-export.jsonl > witan-work-only.jsonl
```

Then use `witan-work-only.jsonl` as the source everywhere below, in place of
the store path. Nothing is removed from your local store by any of this.

**3. Preview the merge:**

```bash
witan migrate merge ~/.local/share/witan/graph.omni --to ol --dry-run
```

`--to ol` names the target block, so the destination is on the command line
rather than in your environment. Read the decisions: `added` should be roughly
the row count of your store, and `updated` should be small. A large `updated`
on a first migration means slugs are colliding that shouldn't — stop there.

This run has no watermark to compare against, so it cannot tell you whether any
collision is a divergence — and being a dry run it records none either, so
step 4 is equally blind. The first run that can report divergence is a merge
*after* step 4 has succeeded. See [Divergence](#divergence).

**4. Run it:**

```bash
witan migrate merge ~/.local/share/witan/graph.omni --to ol
```

Your store is exported locally and the rows ship through the deployment's
`store_merge` tool in batches, written **as you**, under your own credential.
Batches commit independently, so a failure part-way leaves earlier batches
applied — just re-run, the merge is idempotent.

"As you" now covers attribution as well as authorization. A local store writes
`author` from `WITAN_AUTHOR` / git `user.name` / `$USER`, while the deployment
resolves it from your token's `preferred_username` — two namespaces that never
converge. Rows carrying your local name are restamped to your deployed identity
as they arrive, so the history you migrate is owned by the same identity that
owns everything you write afterwards, and `memory_delete` (author-only) still
works on it.

Rows authored by anyone else are left exactly as they are. That matters for the
two merges below: bringing in a teammate's export through your credential does
not reattribute their work to you.

> **Merged before witan-council 0.23.0?** Those rows kept your local name, and
> `memory_delete` refuses them — permanently, since your deployed identity can
> never match. Re-merging will not fix it: reconciliation is
> newest-record-wins, so a re-sent row loses to its own already-applied copy.
> Repair them in place instead:
>
> ```bash
> witan migrate claim-authorship            # dry by default; --was defaults
>                                           # to your local author
> witan migrate claim-authorship --apply
> ```

**5. Verify by slug, not by search:**

```bash
witan whoami
witan memory --kind lesson --all-repos | head    # a listing, not a search
witan task <a-task-slug-you-recognise>
witan tasks --all-repos | head
```

`witan memory "<some words>"` will very likely return *No memories.* on a
freshly-populated graph even when every row is present — that is a BM25
property of a small corpus, not a failed merge
([why](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/store-merge-findings.md#search-looks-broken-on-a-near-empty-graph--verify-by-slug)).
Listings and `witan task <slug>` read the graph directly and do not go through
BM25, which is what makes them usable here.

**6. Keep your local store until you have verified.** It is the backup.

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
2. Take stock before sending anything. The destination is an org-wide shared
   graph and the merge is all-or-nothing, so inventory the local store first
   with the export + `jq` repo-count command in step 2 of that section. Show me
   the full list of repos and their row counts, and flag anything that looks
   personal rather than work — a non-org repo, a side project, a personal
   checkout path. Do NOT decide this yourself and do NOT assume `(no repo)`
   rows are personal (they are mostly general engineering lessons). Ask me
   which, if any, to leave behind.
3. If I name anything to exclude, build the filtered export with the two-pass
   `jq` recipe in that same step (nodes AND the edges touching them) and use
   the filtered `.jsonl` as the source from here on. Tell me the before/after
   row counts.
4. `witan migrate merge <source> --to ol --dry-run`, where `<source>` is
   `~/.local/share/witan/graph.omni` or the filtered export. Report the
   added/updated/kept counts. STOP AND ASK before going further if `updated` is
   more than a handful — on a first migration that means slugs are colliding
   that should not, and each one silently drops a record.
5. Once I approve, run the same command without --dry-run.
6. Verify with `witan memory --kind <kind> --all-repos` listings and
   `witan task <slug>` on two or three slugs from the dry-run decision list.
   Do NOT verify with `witan memory "<words>"` — that is a BM25 search, and it
   returns nothing on a small corpus even when every row landed, so an empty
   result is not evidence of anything.
7. Report what landed. Do not delete, move, or clean up my local store or any
   export file — they are the backup until I say otherwise.

If any step fails, stop and show me the error rather than retrying or working
around it.
```

The guardrails are the point: the merge is idempotent, so re-running is safe,
but what gets sent to a shared graph is not undoable by re-running, and a large
`updated` count and a "search finds nothing" reading are both things an agent
will otherwise sail past.

## Cross-machine merge and machine migration

Same command, a store or a named target on each end:

```bash
# two named local targets — neither end is a path anyone types
witan migrate merge --from personal --to work

# by path: merge each machine's store into a shared third one
witan migrate merge machine-a.omni --target-uri combined.omni
witan migrate merge machine-b.omni --target-uri combined.omni

# machine migration: the target starts empty and is created automatically
witan migrate merge old-machine.omni --target-uri new-machine.omni

# from a store that can't travel — hand over its export instead
omnigraph export --store ~/.local/share/witan/graph.omni > alice.jsonl
witan migrate merge alice.jsonl --target-uri combined.omni
```

Preview with `--dry-run` first. The merge then verifies itself:

```
Merged … into …: 2 added, 1 updated, 1 kept (target already newer-or-equal), 5 rows loaded.
Verified: all 6 source row(s) accounted for (2 added + 1 updated + 1 kept + 2 edge/unkeyed).
```

Every source record lands in exactly one of those buckets, so a total that
does not reconcile means rows that were read from the source and never
decided or never written — which is what a merge that stopped part-way looks
like. It reports `NOT verified` with the shortfall and says to re-run; the
merge is idempotent, so rows that already landed are kept rather than
duplicated. Interrupting a merge reports the same way rather than only saying
it was interrupted.

This is the verification to use against a deployed target. The older manual
check needs an export of the target:

```bash
omnigraph export --store <target> | jq -r .type | sort | uniq -c
```

Type counts in the target should equal the union of the sources' counts, minus
collisions resolved in the target's favour. It only works store-to-store: the
deployment's data tier is ClusterIP-only and an ordinary user holds no
omnigraph bearer token for it, which is why the merge reports its own
accounting.

## Flags

```
witan migrate merge [SOURCE] [--from <name>] [--to <name>] [--target-uri <uri>] [--dry-run]
```

| Flag | Means |
|---|---|
| `SOURCE` | Store URI to merge **from**: local path, `s3://`, `file://`, `http(s)://`, or a local `omnigraph export` `.jsonl`. |
| `--from <name>` | A `[targets.<name>]` block's `server`, in place of `SOURCE`. A target with only a `remote_url` is refused — nothing local to export. |
| `--to <name>` | A `[targets.<name>]` block as the destination: through its deployment if it has a `remote_url`, into its `server` store if not. |
| `--target-uri <uri>` | A destination store URI. Defaults to your configured store. Mutually exclusive with `--to`; `.jsonl` is refused (a target is a graph, not a snapshot). |
| `--dry-run` | Print the per-slug decisions, write nothing. Reports divergence; records no watermark. |

Notes:

- A `.jsonl` **source** must be a readable local file — witan fetches no remote
  exports. Download it first (`aws s3 cp …`) and pass the path.
- A missing local destination is created and schema-applied; a missing remote
  one is assumed to exist.
- A deployed graph addressed by URI is
  `http(s)://<host>:<port>/graphs/<graph-id>` — the `/graphs/<id>` part is
  required.
- Against a deployment, `--target-uri` is refused: the destination is that
  deployment's own graph, resolved server-side. Use `--to <name>`.
- Merging is **repeatable**. A re-run against an already-merged target loads
  nothing, because every source row loses reconciliation to its own applied
  copy. Safe on a schedule.
- Reconciliation covers nodes only. Edge rows (`Tagged`, `ParentOf`, …) have no
  slug and pass through unreconciled, same as raw `--mode merge`.

## Divergence

Newest-record-wins is a whole-**record** decision, and several witan fields are
append-only logs rather than values — `WorkflowProject.description`, which
accretes status blocks, most of all. When both stores have written the same node
since they last agreed, keeping the newer record does not resolve a stale value;
it deletes the other side's text.

Every merge therefore records a **watermark** for the pair of stores: the newest
timestamp in the source, and the newest that will be in the target once this
merge's winners land. The next merge uses it to name the nodes both sides have
written since:

```
2 node(s) changed on BOTH sides since the last merge (2026-08-19T19:46:00Z).
Newest-record-wins keeps one side and drops the other's edit …
  WorkflowProject   wp-witan-multi-user-service-deployment-dcf6ee
    source 2026-08-19T19:49:00Z  target 2026-08-19T22:06:00Z  -> kept target
```

Nothing is merged for you. Reconcile the named slugs by hand — read both sides,
write the combined value to whichever store you want to win, and re-run — then
the merge resolves them on its own rule.

- **The first merge of a pair has no watermark and says so.** That is "cannot
  tell", not "nothing diverged"; until one is recorded, diff the projects you
  care about yourself.
- `--dry-run` reports divergence but records no watermark: the mark describes a
  target with this merge's winners in it, and a dry run wrote none of them.
- Marks live in `~/.config/witan/merge-watermarks.json`
  (`$WITAN_MERGE_WATERMARKS`), beside the token cache, keyed by source store and
  destination. Per-machine, and losing the file costs one merge's reporting.
  Local paths are keyed by their resolved absolute path, so `graph.omni`,
  `../graph.omni` and `file:///…/graph.omni` share one mark rather than three.
- **A merge that fails part-way leaves no mark.** The standing one is retired
  before the first batch commits and a fresh one installed only on success,
  because batches commit independently: rows from a half-finished merge are
  already in the target, and a mark that predates them would read those rows as
  an independent target edit. The next run says it cannot tell, which is true.
  Re-run the merge to get back to a marked state.
- Each side is compared against its own mark, which keeps the source's clock
  out of the target's threshold and vice versa — a laptop and a cluster do not
  agree closely enough for a cross-clock comparison. One documented exception:
  the rows a merge loads carry their source timestamps into the target, so the
  target mark is raised to cover them (otherwise every row a merge added would
  come back as a target edit). Under a source clock running ahead, that leaves
  a blind window the width of the skew in which a genuine target edit is not
  reported.

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

[`store-merge-findings.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/store-merge-findings.md) — the verified `--mode
merge` collision behaviour, the slug-collision probability, and the BM25
measurement behind "verify by slug, not by search".

## References

- [`deployed-witan-onboarding.md`](deployed-witan.md) — the other
  half of the cutover: pointing your CLI and agent at the deployment.
- [ADR-0007](../explanation/decisions/0007-local-to-shared-store-migration-transport.md) — why the
  local → shared path is a client-side export merged through the MCP tier.
- ol-infrastructure `docs/witan-admin-break-glass-runbook.md` — the
  `witan-break-glass` pod and its `svc-witan-admin` token.
