# Store migration runbook: local → shared, and cross-machine merges

How to move a witan `.omni` store between locations — onto the shared
multi-tenant server (per
[ADR-0009](https://github.com/mitodl/ol-infrastructure/blob/main/docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md)),
between two of your own machines, or merged with a teammate's store. All three
are the same operation with a different target.

## The rule: never `mv`/copy a store

omnigraph's Lance storage engine embeds absolute paths in its on-disk files.
Copying, `mv`-ing, rsync-ing, or tarring a `.omni` directory to a new location
produces a store that fails to open (or silently reads/writes the wrong
paths). The only supported way to relocate or combine data is:

```bash
omnigraph export --store <source> > data.jsonl
omnigraph init --schema <schema.pg> <target>   # skip if target already exists
omnigraph load --store <target> --data data.jsonl --mode merge --as <actor>
```

This holds whether the source and target are both local paths on one machine,
one is `s3://`, or one is the shared server's store.

## When to use this

| Scenario | Source | Target |
|---|---|---|
| Local → shared (ADR-0009) | your local store's **export** | the deployed graph, from inside the cluster — see [below](#local--shared-the-cutover) |
| Cross-machine merge | two machines' independent local stores | either one, or a fresh third store |
| Machine migration | old machine's local store | new machine's local store |

All three use the identical three-command sequence above. "Migration" (one
source, empty target) and "merge" (two sources with independent history) are
the same command — `--mode merge` into an empty target behaves like a plain
import, so there's one procedure to remember rather than three.

The first row is the one with a network boundary in the middle, and it has its
own section: the deployed data tier is ClusterIP-only, so the merge runs
*inside* the cluster and your store reaches it as an export file.

For a **merge** of two non-empty stores, run the load step twice, once per
source, into the same target:

```bash
omnigraph init --schema schema/schema.pg <target>
omnigraph load --store <target> --data machine-a.jsonl --mode merge --as <actor>
omnigraph load --store <target> --data machine-b.jsonl --mode merge --as <actor>
```

Order doesn't matter for non-colliding data. It matters a great deal if two
records share a slug — see below.

## Recommended: `witan migrate merge`

Rather than the raw three-command dance above, use:

```bash
witan migrate merge <source> [--target <target>] [--dry-run]
```

`source`/`target` are store URIs (local path, `s3://`, or `file://`); `target`
defaults to your currently configured store. It exports both sides,
auto-creating a missing *local* `target` first (schema-applied, empty — same
as `witan serve` on a fresh machine; a missing *remote* `target` is assumed to
already exist and is left for the export step to fail against, not silently
created), then adds the one thing raw `omnigraph load --mode merge` doesn't
have: a **reconciliation strategy**. For every node present in both stores
(matched on type + slug), it keeps whichever has the newer timestamp
(`updated_at`, falling back through the fields in `_RECONCILE_TS_FIELDS`,
`witan/server.py`) instead of blindly taking whichever file happened to load
last. Rows that only exist in one side are always kept; rows the target
already has at an equal-or-newer version are left alone.

This makes it **repeatable**: run it again with the same source against an
already-merged target and it loads nothing (every source row loses
reconciliation to its own already-applied copy) — safe to wire into a cron job
or run after every session, not just as a one-time cutover.

```bash
# preview before touching anything
witan migrate merge ~/.local/share/witan-laptop-b/graph.omni --dry-run

# cross-machine merge: run once per machine's store against a shared target
witan migrate merge machine-a.omni --target combined.omni
witan migrate merge machine-b.omni --target combined.omni

# machine migration: same command, target starts empty — a missing local
# target is created and schema-applied automatically, no separate init step
witan migrate merge old-machine.omni --target new-machine.omni

# merge from a store that can't travel: its export can (see "never mv" above)
witan migrate merge alice-export.jsonl --target combined.omni

# a deployed graph, addressed as a server (from inside the cluster, or over a
# port-forward) — not `--store`, which omnigraph 0.8.1 rejects for http(s)
witan migrate merge alice-export.jsonl --target http://127.0.0.1:8080/graphs/council
```

`source` accepts a plain local path, `s3://`, an explicit `file://` local URI,
an `http(s)://` omnigraph-server — or **the path to a local `omnigraph export`
JSONL**. Anything ending `.jsonl` is read as an export rather than
re-exported; the suffix is unambiguous, since a store is always a directory.
That form is what crosses machine boundaries, per the "never `mv`" rule at the
top: a `.omni` directory cannot be copied, but its export can.

An export must be a readable local file — `merge` never fetches one over the
network. An export is bytes rather than a store, so a remote one is downloaded
by whatever already holds credentials for it (`aws s3 cp …`, `curl`) and passed
as a path; a `s3://…/x.jsonl` source is refused with that instruction rather
than a misleading "no such file".

`target` takes the same set *minus the export form* — merging appends to a
graph and an export is a snapshot of one, so a `.jsonl` target is refused
rather than auto-created as a store under that name — and defaults to your
configured store. A missing local `target` is auto-created; a missing remote
`target` is assumed to already exist and is left alone (same as `witan serve`
on a fresh machine). A deployed graph is
`http(s)://<host>:<port>/graphs/<graph-id>` — or simply the configured store,
when the command runs somewhere `WITAN_MEMORY_URI` already points at the
server. Both spellings of the configured graph use the configured
`WITAN_MEMORY_TOKEN`; any *other* remote store falls back to an ambient
`OMNIGRAPH_BEARER_TOKEN`. A remote target with no graph id at all
(`http://host:8080`, no `/graphs/<id>`) is an error naming what's missing.

Reconciliation only applies to nodes (anything with a `slug`) — edge rows
(`Tagged`, `ParentOf`, ...) have no slug and pass through the same load
unreconciled, same as raw `--mode merge`.

The rest of this doc explains *why* the command works this way — what raw
`--mode merge` actually does on a collision, and the manual procedure to fall
back to in an environment without the `witan` CLI installed (just `omnigraph`
itself).

## Verified: what `--mode merge` actually does on a slug collision

Every witan node type (`Memory`, `Task`, `WorkflowProject`, `WorkflowSession`,
`WorkflowTrace`, `Topic`, `CodeBranch`) declares `slug: String @key` in
`schema/schema.pg`. This was checked directly against the installed
`omnigraph` v0.8.0 binary rather than assumed, because the two plausible
behaviors — silent overwrite vs. a loud error — have very different
implications for a runbook people will actually run against real stores:

```bash
# two independent stores, same slug, different content
omnigraph init --schema schema.pg store-a && omnigraph init --schema schema.pg store-b
omnigraph mutate --store store-a insert_memory --params '{"slug":"mem-x-abc123", "content":"from A", ...}'
omnigraph mutate --store store-b insert_memory --params '{"slug":"mem-x-abc123", "content":"from B", ...}'
omnigraph export --store store-a > a.jsonl && omnigraph export --store store-b > b.jsonl

omnigraph init --schema schema.pg merged
omnigraph load --store merged --data a.jsonl --mode merge --as tmacey
omnigraph load --store merged --data b.jsonl --mode merge --as tmacey
omnigraph export --store merged | grep mem-x-abc123
# → {"content":"from B", ...}   -- B silently replaced A. No error, no warning, exit 0.
```

**Result: `load --mode merge` on a `@key` collision is a silent, unconditional
last-loaded-wins overwrite.** No error, no warning, exit code 0. This differs
from the single-row `omnigraph mutate insert_*` path used by witan's own MCP
tools at runtime, which no-ops on a duplicate key (documented for `Topic` in
`_topic_slug`'s docstring, `witan/server.py:250-254`) — **bulk `load` and
single-row `mutate insert` are different code paths with different collision
semantics.** Don't reason from one to the other.

For contrast, the other two `load` modes were also checked and are worse fits
for this job:
- `--mode append` lets two rows with the same `@key` slug coexist —
  corrupts the uniqueness invariant outright; a later `get_memory`-by-slug
  lookup against a duplicated key has undefined behavior. Never use it here.
- `--mode overwrite` replaces the *entire table* for every node type present
  in the loaded file, not just the colliding rows — destructive at the wrong
  granularity for a merge (it's what `witan migrate storage` uses, for the
  different case of reimporting one store's own data into a rebuilt copy of
  itself, `witan/server.py:510-522`).

`merge` is the only mode of the three that does the right thing for
non-colliding data and the empirically-confirmed-worst-but-tolerable thing
(silent overwrite, not corruption) for colliding data — which is why ADR-0009
specifies it.

## Is the collision risk actually negligible?

Mostly yes, but the ADR's framing ("negligible from title+random-hex slugs")
undersold the failure mode, which is why this was verified rather than
assumed. From `_make_slug` (`witan/server.py:226-231`):

```python
def _make_slug(kind: str, title: str) -> str:
    prefix = _KIND_PREFIX.get(kind, "mem")
    sanitised = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48]
    short_id = uuid.uuid4().hex[:6]
    return f"{prefix}-{sanitised}-{short_id}"
```

`Memory`, `Task`, `WorkflowProject`, and `WorkflowSession` get a
`uuid.uuid4().hex[:6]` suffix — 24 bits (~16.8M values) from an OS CSPRNG, with
**no** application-level uniqueness check before insert (unlike `Topic` and
`CodeBranch`, which are deliberately deterministic and check-then-insert).
Two records only compete for that 24-bit space if they share both `kind` and
the *identical* sanitized/truncated title — collision space is partitioned
per `(kind, title)`, not global.

The structurally riskiest case is `WorkflowSession`: its "title" input is
actually the parent `project_slug` (`witan/server.py:2199`), so every session
under one project shares the exact same sanitized prefix by construction —
they compete only on the 6 hex chars. Using this repo's real local store as a
scale reference (`omnigraph export` against `~/.local/share/witan/graph.omni`):
534 tasks, 129 memories, 93 workflow sessions across 32 projects — i.e. at
most a few dozen sessions for even the busiest single project. Merging two
machines with, generously, 50 sessions each on the same hot project:

```
P(collision) ≈ 1 - exp(-(50 × 50) / 16_777_216) ≈ 0.015%
```

Negligible in the sense of "won't happen in practice." **Not** negligible in
the sense of "safe to skip checking for," because the confirmed failure mode
is a silent overwrite with zero error signal — you would not find out a
collision happened by watching the migration run; you'd find out later when a
memory or task you know you wrote is missing. Cheap insurance beats a
low-probability, high-cost, undetectable failure:

## Manual fallback: pre-merge collision check without the witan CLI

`witan migrate merge` (above) does this automatically, keyed on timestamps
rather than just flagging the collision. Use the manual version below only in
an environment with the raw `omnigraph` binary but not the `witan` CLI. Diff
the slug sets of every source export against the current target export (empty
target ⇒ empty diff, still worth running so the check is one habit, not a
conditional one):

```bash
omnigraph export --store <target> | jq -r 'select(.data.slug) | .data.slug' | sort > target-slugs.txt
for src in machine-a.jsonl machine-b.jsonl; do
  jq -r 'select(.data.slug) | .data.slug' "$src" | sort > "$src.slugs"
  echo "=== collisions in $src vs current target ==="
  comm -12 target-slugs.txt "$src.slugs"
done
```

Any output means a real collision — resolve it by hand (rename one side's
slug in its export file, or pick which content wins) before loading. A
`Topic` hit in this diff is not a real problem (it's a deterministic,
content-equivalent duplicate by design — see `_topic_slug`'s docstring); a
`Memory`/`Task`/`WorkflowProject`/`WorkflowSession` hit is a genuine two
different records, one is about to silently disappear — this is exactly what
`witan migrate merge`'s newest-wins reconciliation resolves for you.

## Local → shared: the cutover

Moving your local store onto the deployed service (ADR-0009). **Do it
yourself** — no kubectl, no port-forward, no AWS credentials:

```bash
witan target add ol --remote-url … --oidc-issuer …     # once, if you haven't
witan login --target ol                                # once, if you haven't

export WITAN_TARGET=ol                                 # ← selects the deployment
witan whoami                                           # confirm it, before merging
witan migrate merge ~/.local/share/witan/graph.omni --dry-run
witan migrate merge ~/.local/share/witan/graph.omni
```

The first two lines are step 1 and 2 of
[`deployed-witan-onboarding.md`](deployed-witan-onboarding.md), which has the
actual hostnames — do that first if you have not.

**`export WITAN_TARGET` is load-bearing, and its absence fails quietly.**
`migrate merge` picks its destination the same way every other command does —
from the environment and the checkout — so a target that matches neither
resolves to *no* remote, and the merge runs happily against your local store
instead of the deployment. There is no error; the rows just go nowhere useful.
Setting `WITAN_TARGET` pins it regardless. You cannot use `--target` here:
`migrate merge --target` names a *store to merge into*, not a config target,
and against a deployment it is refused outright (see below).

If the target carries a `match_*` selector that covers your checkout (the
onboarding doc's example uses `match_orgs`), it selects itself inside those
repos and the export is unnecessary there — `witan whoami` is what tells you
which case you are in. Run it from the directory you will merge from.

With a deployment configured (`remote_url`), `merge`
exports your store locally and ships the rows through the deployment's
`store_merge` tool in batches. The server reconciles each batch against the
shared graph and writes the winners — **as you**, using your own actor's
credential, evaluated by Cedar like any other write you make. That is the point
of this path: under the in-cluster alternative below every row lands in the
audit trail as `svc-witan-admin`.

Two things behave differently here than against a local store:

- **`--target` is refused.** The target is the deployment's own graph, resolved
  server-side; a client never names a store address. Unset `remote_url` to
  merge between stores you address yourself.
- **Batches commit independently.** A failure part-way leaves earlier batches
  applied. Just re-run — reconciliation makes an already-applied row lose to
  its own copy, so nothing double-writes. It is recoverable, not atomic.

Verify with `witan memory show <a-slug-you-recognise>`, then run
`witan migrate repo-keys` and `witan migrate topics` **once** after everyone
has merged — those are in-cluster admin commands (below), not self-service.

Keep your local store until you have verified. It is the backup.

### Fallback: the in-cluster path

Use this when the MCP tier is unavailable, or for a bulk merge on someone
else's behalf. Two constraints shape it:

- **The data tier is ClusterIP-only** and never exposed (ADR-0009), so the
  merge runs *inside* the cluster as `svc-witan-admin` via the
  `witan-break-glass` pod (ADR-0005 path b).
- **Your store cannot travel.** Lance embeds absolute paths, so
  `~/.local/share/witan/graph.omni` cannot be copied, tarred, or staged in a
  bucket. Its export can, and that is what you hand over.

Every write lands as `svc-witan-admin` rather than as the user, which is why
this is the fallback. See
[ADR-0007](adr/0007-local-to-shared-store-migration-transport.md) for the full
reasoning, and ol-infrastructure's `docs/witan-admin-break-glass-runbook.md`
for the pod itself.

### 1. You: export your store

```bash
omnigraph export --store ~/.local/share/witan/graph.omni > "$USER-witan.jsonl"
wc -l "$USER-witan.jsonl"     # sanity: should be thousands of rows, not zero
```

Stop writing locally once you have exported — anything you write afterwards is
outside the merge. Coordinate this with pointing your CLI at the deployment
(see [`deployed-witan-onboarding.md`](deployed-witan-onboarding.md)), so
there is no window where you are still writing to a store nobody will merge
again.

### 2. Operator: open a break-glass pod and stream the file in

```bash
kubectl -n witan create job witan-bg-$(date +%s) --from=cronjob/witan-break-glass
JOB=witan-bg-<id>      # from the output above

kubectl -n witan exec -i job/$JOB -- sh -c 'cat > /tmp/alice.jsonl' < alice-witan.jsonl
kubectl -n witan exec -it job/$JOB -- wc -l /tmp/alice.jsonl   # confirm it landed intact
```

`kubectl exec -i` is the ingress mechanism, not `kubectl cp` and not S3: the
break-glass pod declares no volume and no ServiceAccount, so it holds neither
bucket credentials nor an `aws` binary. The same idiom is used by
ol-infrastructure's storage-format upgrade runbook. The file lands in the
container's ephemeral writable layer — fine for an export, but check the size
against the pod's 1Gi memory limit for an unusually large store.

### 3. Operator: dry-run, review, merge

The pod's `WITAN_MEMORY_URI` already points at the in-cluster omnigraph-server
and `WITAN_MEMORY_TOKEN` carries the `svc-witan-admin` credential, so the
target needs no flag:

```bash
kubectl -n witan exec -it job/$JOB -- witan migrate merge /tmp/alice.jsonl --dry-run
```

Read the per-slug decisions before going further — in particular that the
`added` count is roughly the row count you exported, and that `updated` is
small. A large `updated` count on a first migration means slugs are colliding
that shouldn't; stop and investigate rather than overwriting.

```bash
kubectl -n witan exec -it job/$JOB -- witan migrate merge /tmp/alice.jsonl
```

### 4. Operator: post-merge migrations, then clean up

These are no-ops until real data lands, which is what step 3 just did:

```bash
kubectl -n witan exec -it job/$JOB -- witan migrate repo-keys
kubectl -n witan exec -it job/$JOB -- witan migrate topics
kubectl -n witan delete job $JOB
```

### 5. You: verify through the deployment

Spot-check slugs you know you wrote, through the MCP endpoint as *yourself* —
this checks the merge and your own registration in one step:

```bash
witan whoami
witan memory show <a-slug-you-recognise>
witan tasks --all-repos | head
```

Keep your local store until you have done this. It is the backup.

### Repeating this per user

Run steps 1–3 once per person, in sequence, into the same graph. Order only
matters for genuine slug collisions, and the merge resolves those newest-wins
either way. Run step 4 once at the end rather than after every user.

### If you have cluster credentials

The same commands work from a laptop over a port-forward, which is a
convenience rather than a second supported path — it needs your own bearer
token out of the `actor-tokens` Secret, which most users cannot read:

```bash
kubectl -n omnigraph port-forward svc/omnigraph-server 8080:8080 &
OMNIGRAPH_BEARER_TOKEN=<your-actor-token> \
  witan migrate merge ~/.local/share/witan/graph.omni \
  --target http://127.0.0.1:8080/graphs/council --dry-run
```

Note this form merges from the *store* directly — no export step, since the
store is on the same machine as the command.

## Procedure

The generic procedure, for merges that don't cross into the cluster (the
cross-machine and machine-migration rows above):

1. **Back up first.** Do not delete or touch the source store(s) until the
   merge is verified. `cp -r` a local store aside if you want an extra
   safety net beyond the export file itself (the copy is not usable as a
   store per the "never `mv`" rule above — it's a cold backup only, to
   `export` from again if the merge needs to be redone).
2. `witan migrate merge <source> --target <target> --dry-run` — review the
   per-slug decisions (`added` / `updated` / `kept-target`).
3. `witan migrate merge <source> --target <target>` — run it for real. Repeat
   once per source store when merging more than one.
4. **Verify:** row counts per type in the target should equal the union of
   the sources' counts minus any collisions resolved in the target's favor —
   `omnigraph export --store <target> | jq -r .type | sort | uniq -c` against
   the same on each source.
5. Point `WITAN_MEMORY_URI` (or `config.toml`'s `server =`) at the new target
   and spot-check a few known slugs with `witan memory show <slug>` /
   `witan task show <slug>` before decommissioning the source.

Without the `witan` CLI available (raw `omnigraph` only), use the manual
`export` → collision-check → `init` → `load --mode merge` sequence described
above instead, resolving any real collision by hand before loading.

## References

- [ADR-0009](https://github.com/mitodl/ol-infrastructure/blob/main/docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md) —
  the shared-service decision this runbook implements the migration path for.
- [ADR-0007](adr/0007-local-to-shared-store-migration-transport.md) — why the
  local → shared path is an in-cluster merge from a handed-over export, and
  what was rejected (exposing the data tier; an MCP-tier bulk-import tool).
- [`deployed-witan-onboarding.md`](deployed-witan-onboarding.md) — the other
  half of the cutover: pointing your CLI and agent at the deployment. Sequence
  it against the merge so you aren't writing locally during the handover.
- ol-infrastructure `docs/witan-admin-break-glass-runbook.md` — the
  `witan-break-glass` pod the in-cluster steps run in, and how to provision the
  `svc-witan-admin` token it authenticates with.
- `witan/server.py:226-259` — `_make_slug` / `_topic_slug`, the slug-generation
  code this runbook's collision analysis is based on.
- `mcp/servers/witan/schema/schema.pg` — `@key` declarations for every node type.
- `witan/server.py` `merge_store` / `witan/cli/migrate.py` `merge` — the
  `witan migrate merge` command this runbook recommends; see
  `tests/test_migrate.py` for its reconciliation test coverage.
- `witan/cli/migrate.py` / `witan migrate storage` — the related but distinct
  same-store format-rebuild command; reuses `export`/`init`/`load` too, but
  with `--mode overwrite` against a freshly-rebuilt copy of the same data,
  not a merge of independent stores.
