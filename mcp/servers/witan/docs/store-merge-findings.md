# Store merge: what was verified, and why the runbook is shaped that way

Background for [`migration-runbook.md`](migration-runbook.md). Nothing here is
a step to run — it is the evidence behind the steps.

## `omnigraph load --mode merge` silently overwrites on a slug collision

Every witan node type (`Memory`, `Task`, `WorkflowProject`, `WorkflowSession`,
`WorkflowTrace`, `Topic`, `CodeBranch`) declares `slug: String @key` in
`schema/schema.pg`. Checked against the installed `omnigraph` v0.8.0 binary
rather than assumed, since silent overwrite and a loud error imply very
different runbooks:

```bash
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

**Result: last-loaded-wins, with no signal.** This differs from the single-row
`omnigraph mutate insert_*` path witan's MCP tools use at runtime, which no-ops
on a duplicate key — bulk `load` and single-row `mutate insert` are different
code paths with different collision semantics. Don't reason from one to the
other.

The other two modes are worse fits:

- `--mode append` lets two rows share a `@key` slug — corrupts the uniqueness
  invariant outright. Never use it here.
- `--mode overwrite` replaces the *entire table* for every node type in the
  loaded file. That is what `witan migrate storage` uses, for the different
  case of reimporting one store's own data into a rebuilt copy of itself
  (`witan/server.py:510-522`).

`witan migrate merge` layers newest-record-wins reconciliation on top of
`--mode merge`, which is what turns "silent overwrite" into a decision you can
preview with `--dry-run`.

## The collision risk is small, but undetectable without the reconciliation

From `_make_slug` (`witan/server.py:226-231`), `Memory`, `Task`,
`WorkflowProject`, and `WorkflowSession` slugs end in `uuid.uuid4().hex[:6]` —
24 bits, no application-level uniqueness check before insert (unlike `Topic`
and `CodeBranch`, which are deterministic and check-then-insert). Two records
compete only if they share both `kind` and the identical sanitised, truncated
title, so the space is partitioned per `(kind, title)`.

`WorkflowSession` is the structurally riskiest: its "title" input is the parent
`project_slug` (`witan/server.py:2199`), so every session under one project
shares a prefix by construction. Against this repo's real local store — 534
tasks, 129 memories, 93 sessions across 32 projects — merging two machines with
a generous 50 sessions each on one hot project:

```
P(collision) ≈ 1 - exp(-(50 × 50) / 16_777_216) ≈ 0.015%
```

Negligible in the sense of "won't happen." Not negligible in the sense of "safe
to skip checking for": the failure mode is a silent overwrite you would notice
weeks later, when a memory you know you wrote is missing.

## Search looks broken on a near-empty graph — verify by slug

Measured against the CI council graph at N=2 rows: a term common relative to the
corpus matches nothing, and one such term zeroes the whole query.
`'quokkazebra'` returned its row; `'quokkazebra policy'` returned nothing,
because `'policy'` appeared in both stored rows. Real queries are multi-word, so
nearly all of them come back empty.

This is a BM25 property, not a broken index and not a failed merge: as a term's
document frequency approaches the corpus size its IDF goes non-positive and the
row drops out. It clears as the graph fills — 2 → 10 rows made `bundles`,
`omnigraph`, `list` and a three-term query findable.

Two consequences:

- **Merge everyone in one sitting.** A graph that reaches a few hundred rows
  immediately never spends time in the degenerate regime; staggered cutovers
  leave every early adopter with a search that appears dead.
- **Confirm a merge with a `witan memory --kind <kind>` listing or
  `witan task <slug>`**, never with a search — those read the graph directly,
  where `witan memory "<words>"` goes through BM25. See `tests/test_migrate.py`,
  `test_store_merge_rows_are_findable_by_search_not_just_readable`.

## References

- [ADR-0007](adr/0007-local-to-shared-store-migration-transport.md) — why the
  local → shared path merges through the MCP tier, and what was rejected.
- [ADR-0009](https://github.com/mitodl/ol-infrastructure/blob/main/docs/adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md) —
  the shared-service decision the migration path implements.
- `witan/server.py` `merge_store` / `witan/cli/migrate.py` `merge` — the
  implementation; `tests/test_migrate.py` for its reconciliation coverage.
