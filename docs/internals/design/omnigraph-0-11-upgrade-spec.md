# omnigraph 0.11 upgrade: storage format 9 cutover — spec

Status: accepted (spec phase)
Project: `wp-omnigraph-0-11-upgrade-storage-format-9-cutover--e97de6`
Repos: agent-kit, ol-infrastructure

Every claim below marked "verified" was run against the released 0.11.0 binary
(`omnigraph 0.11.0`, linux-x86_64 tarball sha256
`da192e1a050875a93ee642b9df485203a00d8c0d44ca39204439463ad41a766d`) and, where
a comparison matters, the 0.10.0 binary, on local scratch graphs on 2026-09-15.
Nothing here was run against S3 roots or the deployed clusters except read-only
`kubectl get`.

## Goal

Move council, witan-code, the bridge, local stores and the deployed data tier
from omnigraph 0.10.0 (storage format 6) to 0.11.0 (format 9) in one rebuild,
and declare keyed edges in that rebuild so it does not have to happen twice.

0.11 opens only formats 8 and 9. `omnigraph upgrade` refuses cluster-managed
roots, so every deployed graph is exported with 0.10 and loaded into a fresh
format-9 root with 0.11. Commit history and branch topology are not carried
over. For code graphs that is a reindex. For council it is loss of history
only: nothing in witan reads past commits (the `graph_commit_id` it uses is an
in-flight CAS fence, `witan_core/omnigraph.py:1395`) and council uses no
omnigraph branches. **Open decision, needed before Production:** a maintainer
accepts losing council's commit history (tracked on
tk-execute-the-omnigraph-format-9-cutover-by-export-2ff972). No mixed fleet: CLI, server and clients move together, and
rollback is the backed-up format-6 root with the 0.10 image.

## D1. Keyed edges: key every edge type on `(@src, @dst)` (decided)

Syntax (verified; the key goes inside the braces, and a `@key` on the
`edge X: A -> B` line is a parse error):

```
edge Tagged: Memory -> Topic {
    @key(@src, @dst)
    confidence: enum(asserted, inferred)? @index
    ...
}
edge Blocks: Task -> Task { @key(@src, @dst) }
```

Semantics (verified):

- Re-inserting a keyed edge, in one batch or across commits, leaves one row.
  The edge id becomes the canonical `["<src>","<dst>"]`.
- **An upsert replaces the whole row.** A re-insert that omits a property
  nulls it. `load --mode merge` behaves the same.
- `load --mode append` of an existing key fails (`already has this id`). Two
  rows with the same key in one load file fail the whole load in every mode
  (`@unique violation on K.(__src, __dst)`).
- The same edge on two branches converges on merge; differing properties fail
  the merge with `divergent_insert`. witan-code never merges omnigraph
  branches (create, list, delete only), so nothing is exposed today.
- Adding `@key` to an existing edge type is refused by `schema plan`
  (`supported: no`), which is why it must ride this rebuild.
- A property can join the key only if non-nullable. Every witan edge property
  is nullable, so the pair is the only available key, and no edge type
  legitimately holds several rows per pair (code-schema edges carry no
  per-call-site properties, and the indexer already dedupes on
  `(edge, from, to)`).

Applies to every edge type in `schema.pg` and `code-schema.pg`.
`bridge-schema.pg` declares no edges.

Workarounds, re-read against keyed semantics:

| Workaround | After keying |
|---|---|
| `_works_on_step` / `_for_project_step` existence reads (`server.py:3854-3877`, `read.gq:1303-1320`) | Keep (changed in implementation): no longer needed for correctness, but they skip a write on every lease renewal and keep a re-entrant `workflow_session_start` at one commit |
| Branch-migration existence reads in `migrate_repo_keys` (`server.py:1480-1500`) | Remove |
| Newest-wins dedupe in `memory_neighbors` (`server.py:3739-3757`) | Keep: RelatedTo/Contradicts are stored one direction and read both ways, and a→b and b→a are different keys |
| Re-tag check (`server.py:3462-3474`) and `migrate_topics` (`server.py:1107-1112`) | Keep: without it an auto-derived `inferred` Tagged upsert would overwrite an `asserted` one |
| Indexer `_dedupe` (`indexer.py:663`, `bridge.py:450`) | Keep: a duplicate key in one load now fails the batch instead of appending |
| ParentOf re-parent (tk-task-update-parent-never-retracts-the-previous-p-69636e) | Not fixed: a new parent is a new key |

Rule for every edge writer after the cutover: send the full intended row,
because a partial re-link erases the properties it omits.

## D2. Loading a 0.10 export into 0.11 (decided)

Three transforms, applied in this order to every exported row before any
0.11 load:

1. **Relocate `data.id` to top-level `id` on node rows.** 0.11 refuses
   `data.id` (`unknown input field 'id': move data.id to the top-level 'id'
   field`, verified). Upstream's rule:
   `if has("id") then . else .id = .data.id | del(.data.id) end`.
2. **Drop `id` from every edge row** (both `data.id` and any top-level `id`).
   0.10 edge rows carry a ULID `data.id`; relocated, it is refused on a keyed
   edge (`explicit id '01M2...' does not match its canonical @key id
   '["tk-two","tk-one"]'`, verified) and the engine derives the id from the
   key. Since D1 keys every edge type, this is all edge rows.
3. **Collapse duplicate edges per `(type, from, to)` over the whole row set,
   before any chunking.** 0.10 graphs hold duplicates (a local reproduction had
   3 Tagged rows for one pair; the original measurement is in
   les-a-keyed-upserted-node-does-not-make-its-write-id-1a16aa). Keep one whole
   existing row, never a composite of several rows' properties, which would
   store an edge nobody wrote. Winner: highest
   `(confidence rank, created_at, export position)`, with asserted > inferred >
   null and a null `created_at` oldest. Asserted outranks recency because that
   is the invariant the re-tag check protects and the route recall already
   scores by (`server.py:7392`); `created_at` ranks second because it is null
   on every edge written before 2026-09. For memory↔memory edges, which
   `memory_link` always writes asserted, this reduces to the newest-wins
   survivor `memory_neighbors` picks today. Verified: 13 rows collapsed to 9
   and loaded into a fully keyed copy of `schema.pg` with no errors; an older
   asserted Tagged beat a newer inferred one; re-merging the collapsed rows and
   the keyed graph's own 0.11 export left counts unchanged.

Collapse works on the full row set, so it cannot be applied one record at a
time, and duplicates can span load batches.

Not needed (verified on both 0.10 and 0.11): filling omitted optionals with
explicit nulls. An absent key under `load --mode merge` nulls the column for
nodes, lists and edges alike, the same as an explicit `null`, so 0.11's
null-omitting export cannot let a cleared field survive a merge.

Unchanged (verified): the 8192 keyed-load row cap (8193 is refused), so
`witan_core.chunking.LOAD_MAX_ROWS = 8_000` still bounds batches. 0.11 load
accepts 0.10's epoch-millisecond DateTimes and explicit nulls. None of our
schemas uses bare `src`/`dst` constraints or `_`-prefixed properties.

Where it lives in agent-kit: one `witan_core` helper that applies D2 to an
export stream given the target schema's keyed types, called from
`migrate_storage_format` (`server.py:1733-1755`, which today pipes the old
export straight into `load --mode overwrite`) and from `_classify_rows`
(`server.py:1929`), which both merge transports go through. Not in
`load_batch`, which witan-code also uses and which the migrate path skips.
Reconciliation is unaffected: the classifier still finds `data.slug`, an
absent optional reads as `None`, and `_parse_ts` accepts 0.11's naive
DateTime strings.

ol-infrastructure's Job cannot import `witan_core`; it ports the same three
rules (D4) and is tested against the same fixture shape.

## D3. Consumer compatibility

Suite breakage against 0.11 is **not yet measured**
(tk-run-the-witan-witan-code-and-witan-core-suites-a-bfbe54). Known changes to
expect, each checked on the binary:

- Query rows omit null-valued fields.
- Export omits null optionals, and DateTime is a naive ISO string with no `Z`.
- bm25-ordered results tie-break on secondary keys then entity id, and
  unordered traversal `limit`s may return a different valid subset.
- `test_binary_contract.py`: 4 errors from `internal-schema 9 is not in
  _CONTRACTS`; `test_the_binary_on_path_is_the_declared_format` fails until the
  pin bump; `test_export_keeps_an_unset_optional_as_an_explicit_null` fails
  because the key is gone.
- Opening a format-6 store still yields the two substrings the classifier
  matches (`witan_core/omnigraph.py:171`). The message now also offers an
  in-place `omnigraph upgrade --to-format 8` route; it cannot add edge keys,
  so `witan migrate storage` stays export and rebuild.
- `commit list` reports `graph_branch` and microsecond `created_at`,
  `branch list` returns bare strings, `snapshot` prints `entities=N` per table.

`test_binary_contract.py` additions: a format-9 row
`9: {"export_datetime": "iso-string", "keyed_row_cap": 8192}`, plus two new
contract keys with tests: `export_null_optional` (`explicit-null` for 4/6,
`omitted` for 9) and `export_id` (`data` / `top-level`), since the D2 helper
depends on the latter.

Test harness trap: `testsupport/hermetic.py:225-227` puts `$HOME/.local/bin`
first on PATH, so overriding PATH alone silently runs the installed binary.
Running a suite against a different binary needs `HOME` pointed at a
directory whose `.local/bin/omnigraph` is that binary.

## D4. ol-infrastructure storage-format migration Job

Verdict: cannot be armed for 0.10 → 0.11 as written. Verified locally against
a `file://` cluster-layout root with a council graph containing duplicate
edges, driving the Job's own functions with both binaries.

Required changes:

1. `SNAPSHOT_ROW_RE` / `snapshot_tables`: the regex expects 0.8's `rows=N`;
   0.10 and 0.11 print `edge type 'Tagged' ... entities=N`. Today the Job exits
   "parsed no per-table row counts" before exporting. Fix the regex to
   `^(?:node|edge) type '(?P<table>[^']+)'.*\bentities=(?P<rows>\d+)` and the
   fixture at `test_storage_migration.py:187-189`.
2. Apply D2's three transforms before `chunk_export`, reading keyed types from
   the rebuild schema under `/etc/omnigraph/cluster`.
3. `verify`: expected counts come from the transform step (node tables equal
   the baseline, keyed edge tables equal the distinct pair count), and the
   verdict reports how many rows the collapse removed per table. Strict
   equality with the 0.10 snapshot fails every graph that has duplicates.

No change: `check_format_moved` accepts 6 → 9. The parallel `fmt<N>` prefix
approach is upstream v0.11.0 `upgrade.md` "Same graph ID in a parallel cluster
root".

With changes 1 and 2 (unkeyed schema), per-table verify matched and the format
moved 6 → 9 locally. Still needs a CI run of the patched Job with the new image
against a copy of a real format-6 CI root: S3 roots are outside upstream's
qualification, and only real graphs show duplicate volume, tables over 8000
rows through `chunk_export`, 0.11 `cluster validate` on our policy files, and
whether the 20Gi export disk suffices. Only `main` is exported; code-graph view
branches are recreated by the next index.

## D5. Shutdown (decided)

Live state (read-only `kubectl` on `operations-{ci,qa,production}`, namespace
`omnigraph`, matches `data_tier.py`): 1 replica, `Recreate`,
`terminationGracePeriodSeconds: 30` (`data_tier.py:626`), no preStop, all
probes on `/healthz`, no PodDisruptionBudget.

0.11.0 behaviour (verified locally): idle SIGTERM exits 0 in ~10ms. With a
request in flight the listener closes at the signal (new connections are
refused, so `/readyz`'s 503 is never observed on a new connection), the
in-flight request is held to the deadline, then the process exits 2 and that
client gets an empty reply.

Values:

- `OMNIGRAPH_SHUTDOWN_GRACE_SECONDS=30`, as an env var rather than a flag,
  since an unknown flag would crash 0.10 at boot. 30 is the tool-call deadline;
  the measured n=4 concurrent write wall time is 15.54s.
- `terminationGracePeriodSeconds: 35`.
- No preStop: with one replica and `Recreate` there is no endpoint to drain
  to, and a sleep spends from the same budget.
- `witan_core/omnigraph.py:318-330`: comment only. Its "server ignores SIGTERM"
  premise was already stale (`data_tier.py:596-606`). Drain-time connection
  refusals match `_UNAVAILABLE_MARKERS` and are retried; a request cut at the
  deadline fails mid-flight and correctly is not; `_UNAVAILABLE_MAX_WAIT=150`
  covers drain plus boot.

Lands before the cutover. Unverified: that 0.10's `omnigraph-server` ignores
the env var (no 0.10 server binary was available locally); check on CI first.

## D6. Deferred until after the cutover (decided)

Not schema-bound, so none of these needs the rebuild: `bm25(...) as score` in
recall ranking, GQ branch statements replacing witan-code's CLI shell-outs,
filtered nearest widening and selective rrf, readiness on `/readyz`,
`--data-token-trust` in place of restart-on-new-user. Each is its own task
blocked on the pin bump or the cutover.

## Sequencing

1. Before the cutover, in any order: D5 (ol-infra); D1 schema and workaround
   removal, D2 helper, D3 fixes (agent-kit); D4 Job patch plus its CI dry run
   (ol-infra).
2. Pin and format bump (agent-kit), last code step: tag, x86_64 and arm64
   digests, `_OMNIGRAPH_INTERNAL_SCHEMA = 9`, format-9 contract row. The arm64
   digest is not yet fetched.
3. Cutover per environment, CI then QA then Production: suspend
   omnigraph-optimize, omnigraph-cleanup, witan-ci-indexer and
   witan-view-reaper; scale to zero; back up graph roots and `__cluster`; run
   the patched Job with the new image and read the verdict; roll the data tier
   and clients to 0.11; drop the dead `main` graph from `cluster.yaml` in the
   same change (cluster apply does not prune it,
   tk-omnigraph-cluster-apply-never-prunes-graphs-remo-6fe8ef); reindex code
   graphs. Local stores run `witan migrate storage` with the D2 helper.
4. After each environment: verify on the live Deployment (image, grace, env),
   re-verify the cleanup CronJob per graph (0.11 `cleanup` now reclaims
   deleted-branch forks and `optimize` no longer does), re-measure the write
   ceiling and admission gate.
