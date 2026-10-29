# Recovering a store quarantined by an OCC recovery sidecar

A graph that answers every read, every write and `omnigraph repair` with the
same error is quarantined by an unresolved OCC recovery sidecar:

```text
OCC recovery sidecar '01M2V544QY00SSGZA0PSRK3B9X' found original commit id
'01M2V54457QR0XB7ZW47H5V88H' but its manifest delta differs
```

It is a refusal, not a transient failure: nothing clears it but an operator,
and retrying only spends the caller's deadline.

## Which stores this applies to

Only a store still at storage format 8 or 9, written by omnigraph 0.11.x.

omnigraph 0.12.0 removed recovery sidecars altogether ([RFC 0067][v012]): every
write stages its table effects as detached commits that one manifest commit
publishes, so an interrupted write leaves the graph unchanged and there is
nothing to recover. A store at format 14 or later cannot be quarantined this
way. That covers every deployed graph (the 0.13 cutover rebuilt them onto
format 14 on 2026-10-14) and any local store `witan migrate storage` has
converted.

witan releases pinned to omnigraph 0.11 reported this as `StoreQuarantined` and
named the sidecar's operation id. A current witan never gets that far, because
its omnigraph cannot open a format 8 or 9 store at all, so it no longer has
that refusal. You meet the sidecar instead when converting the store:

- `witan migrate storage` fails with "No available omnigraph binary can read
  this store", and the attempt it quotes carries the sidecar error above; or
- `omnigraph upgrade --check` refuses the store as `source_recovery_required`.

Either way, start at step 1 of the recovery below. Step 4 tries a plain
read-write open first, which is enough for a leftover recovery file or an
unfinished schema apply.

**Every `omnigraph` command before step 10 runs with the 0.11 binary.** The
current binary cannot open a format 8 or 9 store read-write, rejects `repair
--confirm` and `--force` (repair has only diagnosed since 0.12), and on 0.14 does
not take `--store`. `witan setup` keeps each outgoing binary beside the new one
as `omnigraph-<version>`, so it is usually already installed:

```bash
OG=~/.local/bin/omnigraph-0.11.0
$OG version   # must print omnigraph 0.11.0
```

If it is missing, download the 0.11.0 release asset for your platform from
[ModernRelay/omnigraph][releases] rather than downgrading the binary witan uses.

## What causes it

Two writers committing against one storage root at the same manifest version,
with no arbiter between them. The loser's recovery sidecar records a delta that
does not match the commit that actually landed, and the store refuses to open
rather than choose.

It was an omnigraph defect, [ModernRelay/omnigraph#602][602]: a sidecar left
`Armed` when its confirmation write is lost, blocking every read-write reopen
store-wide. 0.12.0 fixed it by removing the sidecars. [PR #716][716], which
lets recovery roll a sidecar forward by itself when its effect matches what was
committed, merged two days after the v0.11.0 tag, and no 0.11.x release
followed. So the 0.11.0 binary this page uses refuses a stale `Armed` sidecar
even when the commit it guards landed intact (the 2026-09-18 case), as well as
one that genuinely contradicts the commit. The steps below repair the first and
stop before forcing anything on the second.

Since agent-kit#364, `witan serve` serialises writes to one `s3://` root across
its own threads (`witan_core.omnigraph.store_write_lock`), so two concurrent
tool calls in one process can no longer produce this. **Separate processes still
can.** A second `witan serve` replica, or a CLI run alongside a server, writing
the same `s3://` root is uncoordinated. A shared root wants a served
single-writer target (`https://…`). Direct S3 from more than one process is
not a supported writer topology.

## Recovery

Observed versions: witan 0.36.0, witan-core 0.37.0, omnigraph 0.11.0, storage
format 9. Recorded from the 2026-09-18 recovery of a direct-S3 root.

**First, settle what `<store-uri>` means below**, because the two deployments
are shaped differently and guessing targets a path that does not exist:

- A **direct** store is the configured graph URI, whole. `WITAN_MEMORY_URI`
  and this server's README take that shape, e.g.
  `s3://personal-witan/graph.omni`. There is no root above it and no `graphs/`
  beneath it. The 2026-09-18 incident was on one of these.
- A **cluster-managed** store sits under a storage root, one graph per
  directory: `s3://<bucket>/<root>/graphs/<graph-id>.omni`.

Every command below uses `<store-uri>` for whichever applies. Read it out of
the running configuration rather than reconstructing it: the deployed tier
carries it in the cluster ConfigMap, and a local target has it in
`WITAN_MEMORY_URI` or the named target's `server` entry.

**1. Stop the writers.** Every writer against this root, not just the one that
reported the error: other `witan serve` processes, the CI indexer, the view
reaper, any CLI session. A writer arriving mid-repair re-creates the state you
are about to resolve.

**2. Back the store up before touching anything.** Even step 4's read-write
open changes the store: it removes recovery files and finishes or rolls back an
unfinished schema apply. The repair in step 8 rewrites table heads and is not
reversible.

```bash
aws s3 sync <store-uri> s3://<bucket>/backups/quarantine-$(date -u +%Y%m%dT%H%M)/
```

Verify the copy's object count matches the source before continuing. A backup
you did not check is not a rollback plan.

**3. Preserve the evidence, separately from the backup.** A sidecar is the
only record of what the losing writer intended, and steps 4 and 6 can each
remove it. Keep every one where a later report can reach it:

```bash
aws s3 cp --recursive <store-uri>/__recovery/ ./quarantine-evidence/
```

Also keep the full error text. The operation id is the only handle on the
sidecar, and a truncated log costs a listing of `__recovery/` to recover.

**4. Try a read-write open.** A leftover recovery file that 0.11.0 resolves by
itself, and an unfinished schema apply (a `.staging` root schema object), are
both refused as `source_recovery_required` too, and a read-write open clears
either:

```bash
$OG snapshot --store <store-uri> --json
```

- If it succeeds, the open removed the recovery files (or finished or rolled
  back the schema apply). Skip to step 10.
- If it refuses naming a choice between the live and the staging schema, a
  property-only schema apply was left half done. This page does not cover
  that: see the [upgrade guide][upgrade].
- If it refuses with the OCC sidecar error, carry on. The refusal alone does
  not mean the sidecar contradicts the commit, since 0.11.0 also refuses a
  stale `Armed` sidecar whose commit landed. Step 5 is how you tell.

**5. Read the sidecar before resolving it.** What matters is which tables it
names, the expected version, the post-commit pin, and whether the original
commit it names is present in the graph's commit history. In the 2026-09-18
case the sidecar named four tables at expected version 2 / pin 3 with rollback
outcomes 3→2, the original commit was present carrying exactly the intended
entity changes, and the rollback commit it recorded did not exist. That is the
shape that makes a forward repair safe: the committed state is the intended
state, and only the published table heads disagree with it.

**6. Quarantine the sidecar.** `repair` cannot open the graph while it is
active, so move it aside. Do not delete it: step 3's copy is evidence, not a
substitute.

```bash
aws s3 mv <store-uri>/__recovery/<operation>.json \
  s3://<bucket>/quarantined-sidecars/<operation>.json
```

**7. Preview the repair, and read it.** Never go straight to `--confirm`:

```bash
$OG repair --store <store-uri> --json
```

Expect `suspicious` for exactly the datasets the sidecar named, each with a
published version one behind its Lance HEAD and a `Restore` action, and
`no_drift` for every other dataset.

**Do not force a repair that reports anything else.** `--force` is warranted
only when the drift matches the sidecar's own account: the same tables, the same
one-version gap, and a commit history that already carries the intended changes.
Drift on tables the sidecar never mentioned, a gap of more than one version, or
a missing original commit all mean something other than this failure, and
forcing through them can discard committed rows. Stop and investigate instead.

**8. Repair.**

```bash
$OG repair --store <store-uri> --confirm --force
```

**9. Verify, rather than assume.** Still with the 0.11 binary, since the store
is still on its old format:

```bash
# No drift anywhere, and no sidecar left behind.
$OG repair --store <store-uri> --json
aws s3 ls <store-uri>/__recovery/

# A read-write open succeeds.
$OG snapshot --store <store-uri> --json
```

The 2026-09-18 recovery ended with `no_drift` on all 27 datasets, zero active
sidecars, and reads and writes succeeding.

**Rollback.** If the repair leaves the graph worse, restore step 2's copy with
the writers still stopped, then start again from step 5 with the preserved
sidecar. There is no partial undo of a forced repair.

The restore has to be `--delete`, and that flag is destructive in the direction
you want: without it `aws s3 sync` is purely additive, so every object the
forced repair created survives and you end up with a mixed root that is neither
the pre-repair state nor the post-repair one, while believing you rolled back.

```bash
aws s3 sync --delete \
  s3://<bucket>/backups/quarantine-<stamp>/ <store-uri>
```

**10. Convert the store to the format witan pins.** The store is still format
8 or 9, which witan's omnigraph cannot open. Convert it with the binary
`witan setup` installed, not a newer omnigraph: a newer release converts to a
newer format, and the pinned witan cannot read that either.

Take a fresh backup first, and check it as in step 2. Step 2's copy predates
step 4, so restoring it would also undo the open and any repair. The conversion
rewrites the store in place, and the backup is the only way back if it stops
part-way.

For a local store, `witan migrate storage` does this: it takes a
`.pre-migrate` copy, then runs the conversion. For a direct `s3://` store, back
it up with `aws s3 sync` as in step 2, then:

```bash
omnigraph upgrade --check <store-uri>
omnigraph upgrade <store-uri>
```

A `source_recovery_required` refusal here means a recovery file is still in
place: go back to step 4, and do not move anything aside by hand without going
through steps 5 to 9. The
[upgrade guide][upgrade] covers the other refusals.

This in-place conversion is for a standalone (direct) store only. omnigraph
refuses to upgrade a cluster-managed graph below format 14 in place. That graph
takes the guide's export/import rebuild, with the 0.11 binary doing the export.

**11. Verify through witan itself**, not just the CLI:

```bash
witan memory --target <target> <a term you know is indexed>
witan tasks --target <target>
```

**12. Restart the writers, but not all of them directly.** Bringing every
writer back up against one `s3://` store rebuilds the exact topology this page
opens by calling unsupported, and with it the race that produced the sidecar.
So:

- Resume **one** writer against `<store-uri>` directly, and confirm its writes
  land before going further.
- Any other writer resumes through a served single-writer target
  (`https://…`), which is the supported arrangement for a shared root. One
  that cannot be pointed at a served target stays down until it can.

If only ever one writer was direct, this is just "start it again", but check
rather than assume, because the incident is itself evidence that something was
writing concurrently.

[602]: https://github.com/ModernRelay/omnigraph/issues/602
[716]: https://github.com/ModernRelay/omnigraph/pull/716
[v012]: https://github.com/ModernRelay/omnigraph/releases/tag/v0.12.0
[releases]: https://github.com/ModernRelay/omnigraph/releases
[upgrade]: https://github.com/ModernRelay/omnigraph/blob/v0.13.0/docs/user/operations/upgrade.md
