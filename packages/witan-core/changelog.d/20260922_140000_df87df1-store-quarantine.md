### Added

- **`StoreQuarantined`: a typed refusal for a graph held shut by an unresolved
  OCC recovery sidecar.** The message `OCC recovery sidecar '<id>' … but its
  manifest delta differs` was classified as a generic failure, so every read and
  write retried the full budget and surfaced as opaque prose. It now refuses on
  the first attempt — for reads as well as writes, since the sidecar does not
  clear on its own — carries the sidecar's operation id, and points at
  `docs/guides/store-quarantine-runbook.md`. Deliberately not `needs_repair`:
  `omnigraph repair` cannot open a graph in this state either.

### Changed

- **Writes to an `s3://` store are serialised within the process.** `s3://` was
  grouped with `http(s)://` as unlockable, which is true of flock and false of a
  mutex, so two concurrent tool calls in one `witan serve` could race the same
  manifest version and quarantine the graph (agent-kit#364). `store_write_lock`
  now covers all three tiers: flock for a local path, a per-URI in-process
  `RLock` for `s3://`, nothing for a served store. Writers in *separate*
  processes against one `s3://` root are still uncoordinated — that topology
  wants a served single-writer target. Consequence worth knowing: `store_merge`
  into an `s3://` target now holds that lock across export → reconcile → load,
  so it blocks the process's other writes to that graph for the duration, as it
  has always done for a local target.
