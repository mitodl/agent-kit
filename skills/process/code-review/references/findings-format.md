# Findings format reference

The [`code-review`](../SKILL.md) skill reports findings as a flat,
severity-ordered markdown table — most severe first, across all six
dimensions together rather than grouped by dimension, since a single
correctness bug usually matters more to the reader than every
simplification finding combined.

## Schema

| # | Severity | File:Line | Summary | Failure scenario |
|---|----------|-----------|---------|-------------------|

- **#** — row order, most severe first.
- **Severity** — `high` / `medium` / `low`. `high` = confirmed correctness
  bug, an exploitable security path, a stated goal the diff doesn't meet,
  or a cost with clear, near-term impact. `medium` = confirmed but
  narrower blast radius (an edge case, a rarely-hit path). `low` =
  simplification/style-adjacent, correct either way but worth flagging.
- **File:Line** — exact location, `path/to/file.py:42` — not a range unless
  the finding genuinely spans one (a duplicated block, a whole function).
- **Summary** — one sentence, the claim itself, no rationale.
- **Failure scenario** — mandatory, concrete. For correctness: the specific
  input or state that produces the wrong output or crash. For goal
  alignment: the goal number and the case the diff doesn't meet. For
  security: who the attacker is, what they send, the route it takes, and
  what they get. For an exposure-only security finding (a committed
  credential, a secret written to a log or trace): what is exposed, where
  it lands, and who can read it there, e.g. "a live Stripe secret key in
  `settings/prod.py:14`, readable by anyone with read access to the repo
  and its forks". For simplification/efficiency/reuse: what it costs (the extra query, the
  maintenance burden, the drift risk of duplicated logic) — same column,
  reframed rather than left blank.

A row with no concrete failure scenario doesn't ship — see the
verification pass in [SKILL.md](../SKILL.md#verification-pass).

## Worked example

Reviewing a diff that adds a batch-import endpoint, with stated goals:

1. Import up to 1,000 account records per request (issue #812)
2. Only staff can run imports (issue #812)

| # | Severity | File:Line | Summary | Failure scenario |
|---|----------|-----------|---------|-------------------|
| 1 | high | `importers/views.py:22` | Import endpoint is open to any authenticated user | Goal 2. `BatchImportView` sets `permission_classes=[IsAuthenticated]`; a learner account POSTs a batch and overwrites other accounts' balances. No staff check exists in the view, the serializer, or `import_records()` |
| 2 | high | `importers/batch.py:58` | Unbounded query inside a loop | Goal 1. `import_records()` calls `Account.objects.get(id=r.account_id)` once per record; a 500-record batch issues 500 queries and times out under the request's 30s budget past ~300 records (measured against the existing `/health` timeout config), so 1,000 records never completes |
| 3 | medium | `importers/batch.py:12` | Empty batch raises instead of returning a 400 | `records=[]` reaches `records[0]` on line 12 before the loop, raising `IndexError` instead of the validation error the endpoint's other empty-input paths return |
| 4 | low | `importers/batch.py:80` | Hand-rolled retry loop duplicates `utils/retry.py:retry_with_backoff` | No functional bug, but this loop lacks the jitter and max-attempts cap the existing helper has — future drift risk if one gets fixed and not the other |

If the depth was widened per [SKILL.md](../SKILL.md#depth) (user asked for
a thorough pass), a lower-confidence row still gets `Failure scenario`
filled in, just noted as uncertain in the summary:

| 5 | low (uncertain) | `importers/batch.py:34` | Possible race if two imports for the same account run concurrently | Not confirmed — no lock or transaction observed around the account balance update; would need a concurrent-request test to verify, flagging for awareness rather than asserting as confirmed |

## Dropped and contested findings

When the [skeptic pass](../SKILL.md#skeptic-pass-optional) runs, its
verdicts change the report in two ways.

A finding the skeptic left `uncertain` stays in the table, its severity
tagged `(contested)` and the skeptic's argument appended to the failure
scenario:

| 6 | medium (contested) | `importers/views.py:41` | Import reports success when the ledger API accepts only part of a batch | `ledger.post_batch()` returning `207` passes the `status < 300` check, so a partial write returns 200 to the caller. Skeptic: the ledger service's handlers return only 200, 400 and 500, and no 207 path was found there, but the proxy in front of it wasn't in any checkout, so a 207 from that layer can't be ruled out |

A finding the skeptic refuted moves to a Dropped section after the table,
one line each: the finding, which pass dropped it, why, and the evidence.
The section also lists candidates the verification pass removed. It is
always present when the skeptic pass ran; if nothing was dropped, it says
"nothing dropped".

### Dropped

- `importers/views.py:18` _Import endpoint has no authentication check_
  (skeptic). Auth is enforced at the gateway: the route in
  `ol-infrastructure/src/ol_infrastructure/applications/importer/__main__.py:88`
  applies `openid-connect` with `unauth_action="deny"` to
  `/api/v1/import*`, the only route to the service, and the PR description
  says so.
- `importers/batch.py:71` _Retry loop can run forever_ (verification).
  `max_attempts` is enforced by the only caller, `importers/tasks.py:19`.

If the skeptic ran inline (the fallback when the platform has no subagent
tool), the report says "skeptic pass ran inline" above the table.
