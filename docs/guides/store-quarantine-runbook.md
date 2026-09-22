<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan/docs/store-quarantine-runbook.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan/docs/store-quarantine-runbook.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/store-quarantine-runbook.md).

# Recovering a store quarantined by an OCC recovery sidecar

A graph that answers every read, every write and `omnigraph repair` with the
same error is quarantined by an unresolved OCC recovery sidecar:

```
OCC recovery sidecar '01M2V544QY00SSGZA0PSRK3B9X' found original commit id
'01M2V54457QR0XB7ZW47H5V88H' but its manifest delta differs
```

witan reports this as `StoreQuarantined` and names the sidecar's operation id in
the message. It is a refusal, not a transient failure: nothing clears it but an
operator, and retrying only spends the caller's deadline.

A deployed server does not log that message, because it ends in the transport's
raw error text. The failed call's `mcp.tool_call` line carries
`error_type: StoreQuarantined` and the id as its own `sidecar_operation_id`
field instead.

## What causes it

Two writers committing against one storage root at the same manifest version,
with no arbiter between them. The loser's recovery sidecar records a delta that
does not match the commit that actually landed, and the store refuses to open
rather than choose.

**It is an omnigraph defect, and it is already fixed upstream but not yet
released.** [ModernRelay/omnigraph#602][602] is the same failure: a sidecar left
`Armed` when its confirmation write is lost, blocking every read-write reopen
store-wide. [PR #716][716] makes recovery compare the original committed
snapshot and the Lance transaction at each planned table version and, when they
match, record the operation as `RolledForward` and remove the sidecar by itself.

That PR merged 2026-09-15, two days after the v0.11.0 tag, and no release has
carried it yet, so the binary we pin still bricks.

**It narrows this procedure rather than retiring it.** #716 heals a sidecar
whose planned effect still MATCHES what was committed; its own description is
explicit that "contradictory sidecars still refuse recovery". So once a release
carries it, the common case resolves itself and what is left here is the
genuinely contradictory sidecar, which still opens to nothing and still needs
an operator. Read the steps below as the procedure for that case, not as
scaffolding to delete.

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

**2. Back the store up before touching anything.** The repair in step 7
rewrites table heads and is not reversible. Step 6 only previews it.

```bash
aws s3 sync <store-uri> s3://<bucket>/backups/quarantine-$(date -u +%Y%m%dT%H%M)/
```

Verify the copy's object count matches the source before continuing. A backup
you did not check is not a rollback plan.

**3. Preserve the evidence, separately from the backup.** The sidecar is the
only record of what the losing writer intended, and step 5 moves it out of the
way. Keep it where a later upstream report can reach it:

```bash
aws s3 cp <store-uri>/__recovery/<operation>.json \
  ./quarantine-evidence/<operation>.json
```

Also keep the full error text if you have it (a CLI run prints it). The
operation id is the only handle on the sidecar; if the line has no
`sidecar_operation_id`, the id could not be parsed and a listing of
`__recovery/` is the way to recover it.

**4. Read the sidecar before resolving it.** What matters is which tables it
names, the expected version, the post-commit pin, and whether the original
commit it names is present in the graph's commit history. In the 2026-09-18
case the sidecar named four tables at expected version 2 / pin 3 with rollback
outcomes 3→2, the original commit was present carrying exactly the intended
entity changes, and the rollback commit it recorded did not exist. That is the
shape that makes a forward repair safe: the committed state is the intended
state, and only the published table heads disagree with it.

**5. Quarantine the sidecar.** `omnigraph repair` cannot open the graph while it
is active, so move it aside. Do not delete it: step 3's copy is evidence, not a
substitute.

```bash
aws s3 mv <store-uri>/__recovery/<operation>.json \
  s3://<bucket>/quarantined-sidecars/<operation>.json
```

**6. Preview the repair, and read it.** Never go straight to `--confirm`:

```bash
omnigraph repair --store <store-uri> --json
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

**7. Repair.**

```bash
omnigraph repair --store <store-uri> --confirm --force
```

**8. Verify, rather than assume.** All four:

```bash
# No drift anywhere, and no sidecar left behind.
omnigraph repair --store <store-uri> --json
aws s3 ls <store-uri>/__recovery/

# Real reads and writes through witan itself, not just the CLI.
witan memory --target <target> <a term you know is indexed>
witan tasks --target <target>
```

The 2026-09-18 recovery ended with `no_drift` on all 27 datasets, zero active
sidecars, and reads and writes succeeding.

**Rollback.** If the repair leaves the graph worse, restore step 2's copy with
the writers still stopped, then start again from step 4 with the preserved
sidecar. There is no partial undo of a forced repair.

The restore has to be `--delete`, and that flag is destructive in the direction
you want: without it `aws s3 sync` is purely additive, so every object the
forced repair created survives and you end up with a mixed root that is neither
the pre-repair state nor the post-repair one, while believing you rolled back.

```bash
aws s3 sync --delete \
  s3://<bucket>/backups/quarantine-<stamp>/ <store-uri>
```

**9. Restart the writers — but not all of them directly.** Bringing every
writer back up against one `s3://` store rebuilds the exact topology this page
opens by calling unsupported, and with it the race that produced the sidecar.
So:

- Resume **one** writer against `<store-uri>` directly, and confirm its writes
  land before going further.
- Any other writer resumes through a served single-writer target
  (`https://…`), which is the supported arrangement for a shared root. One
  that cannot be pointed at a served target stays down until it can.

If only ever one writer was direct, this is just "start it again" — but check
rather than assume, because the incident is itself evidence that something was
writing concurrently.

## Afterwards

Add what you saw to [omnigraph#602][602] if it differs from what is already
recorded there. Our 2026-09-18 occurrence matches the production report in that
thread in every respect except the trigger: two concurrent writers rather than a
lost confirmation write. The preserved sidecar from step 3, the commit history
around the original commit, and the repair preview from step 6 are what makes an
occurrence worth adding.

[602]: https://github.com/ModernRelay/omnigraph/issues/602
[716]: https://github.com/ModernRelay/omnigraph/pull/716
