# Findings format reference

The [`code-review`](../SKILL.md) skill reports findings as a flat,
severity-ordered markdown table — most severe first, across all four
dimensions together rather than grouped by dimension, since a single
correctness bug usually matters more to the reader than every
simplification finding combined.

## Schema

| # | Severity | File:Line | Summary | Failure scenario |
|---|----------|-----------|---------|-------------------|

- **#** — row order, most severe first.
- **Severity** — `high` / `medium` / `low`. `high` = confirmed correctness
  bug or a cost with clear, near-term impact. `medium` = confirmed but
  narrower blast radius (an edge case, a rarely-hit path). `low` =
  simplification/style-adjacent, correct either way but worth flagging.
- **File:Line** — exact location, `path/to/file.py:42` — not a range unless
  the finding genuinely spans one (a duplicated block, a whole function).
- **Summary** — one sentence, the claim itself, no rationale.
- **Failure scenario** — mandatory, concrete. For correctness: the specific
  input or state that produces the wrong output or crash. For
  simplification/efficiency/reuse: what it costs (the extra query, the
  maintenance burden, the drift risk of duplicated logic) — same column,
  reframed rather than left blank.

A row with no concrete failure scenario doesn't ship — see the
verification pass in [SKILL.md](../SKILL.md#verification-pass).

## Worked example

Reviewing a diff that adds a batch-import endpoint:

| # | Severity | File:Line | Summary | Failure scenario |
|---|----------|-----------|---------|-------------------|
| 1 | high | `importers/batch.py:58` | Unbounded query inside a loop | `import_records()` calls `Account.objects.get(id=r.account_id)` once per record; a 500-record batch issues 500 queries and times out under the request's 30s budget past ~300 records (measured against the existing `/health` timeout config) |
| 2 | medium | `importers/batch.py:12` | Empty batch raises instead of returning a 400 | `records=[]` reaches `records[0]` on line 12 before the loop, raising `IndexError` instead of the validation error the endpoint's other empty-input paths return |
| 3 | low | `importers/batch.py:80` | Hand-rolled retry loop duplicates `utils/retry.py:retry_with_backoff` | No functional bug, but this loop lacks the jitter and max-attempts cap the existing helper has — future drift risk if one gets fixed and not the other |

If the depth was widened per [SKILL.md](../SKILL.md#depth) (user asked for
a thorough pass), a lower-confidence row still gets `Failure scenario`
filled in, just noted as uncertain in the summary:

| 4 | low (uncertain) | `importers/batch.py:34` | Possible race if two imports for the same account run concurrently | Not confirmed — no lock or transaction observed around the account balance update; would need a concurrent-request test to verify, flagging for awareness rather than asserting as confirmed |
