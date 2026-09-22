# Calibrating the seed

The benchmark is only worth the seed. This is where most of the effort goes.

## Audit the factories first

Read what the app's factories actually produce before assuming they are
realistic. The recurring offenders:

| Factory habit | Production reality |
| --- | --- |
| Rich-text/`TextField` left blank or set to ~200 chars of lorem | Multi-KB HTML, or the reverse — a field you assumed was large is 70 chars |
| One related row per parent (a single default variant, one tag) | A dozen or more |
| Every object attached to one tenant/org | Spread across many, which is what keeps filters cheap |
| Sequence-unique grouping keys (`run_tag`, `slug`) | Shared keys, which is what makes grouping logic do work |

Check the ones that feed the queries in your trace budget. Ignore the rest.

## Calibrate only on independent observables

An **independent observable** is one the change under test does not affect.
Those are legitimate calibration targets because they are identical in both
arms. Tuning the seed until the query you *changed* matches production is
circular and will manufacture whatever result you want.

Build this table and keep it in the final report:

| observable | source | production | seed |
| --- | --- | --- | --- |
| rows per page | response | | |
| nested collection sizes per row | response | | |
| response body size | response | | |
| row-count floor behind the page | trace `IN` lists | | |
| cost of an unchanged query | trace | | |
| per-row counts (enrolments, products, …) | response | | |

Same order of magnitude on every row is the bar. Exact matches are not
required and chasing them tips into fitting.

## Structural realism beats sizing realism

**How rows fan out across joins dominates how wide they are.** A filter across
a multi-valued relation multiplies: rows entering a `DISTINCT` are roughly
(rows matching filter A) × (rows matching filter B) per parent. Get that
structure wrong and no amount of payload tuning rescues the seed.

The mistake worth internalising: putting every tenant record under **one**
tenant. Production spreads a shared object's children across *many* tenants, so
a tenant filter matches only a handful while the prefetch still loads all of
them. That asymmetry — small filter result, large prefetch — is usually the
whole point of the endpoint's cost profile. Collapsing it to one tenant makes
the filter match everything.

In one real case this single structural error drove an unchanged `DISTINCT`
query from production's 65 ms to **1.5-6.2 s**, a 20-95× error, while every
payload size was individually plausible. Restructuring to many tenants brought
it to 39-47 ms.

## Falsify, do not fit

When you infer a seed parameter from a production signal, **test it against a
second, independent signal**. If it breaks that one, discard it.

Worked example:

1. Production's trace showed two gaps in a 4.0:1 ratio; the seed reproduced
   2.1:1.
2. Hypothesis: more many-to-many rows per parent than seeded. Plausible, and it
   would explain the ratio.
3. Test: raise it to 2 per parent. The ratio improves — but an **unchanged**
   query goes from 46 ms to 214 ms against production's 65 ms.
4. The hypothesis is falsified. Revert it.
5. Check the alternatives too: payload size was ruled out because the ratio was
   identical at 2 KB and 8 KB.
6. Conclude honestly — the residual is environmental (RTT, cold cache,
   contention), which also means the local number understates the win.

Report what you ruled out. "We could not reproduce signal X, and here is what
it is not" is a stronger result than quietly dropping it.

## Vary the shape deliberately

Sweep the dimensions you had to guess. Two outcomes, both useful:

- **The delta grows with the dimension** → the cost is driven by it, and
  production (with real payloads and real latency) sees *more* than local.
- **The delta is flat** → the cost is per-object, and local is a fair estimate.

Run the final A/B on at least two different seed shapes. A delta that holds
across shapes is the strongest evidence a local benchmark can produce:

| seed | ref A | ref B | delta |
| --- | --- | --- | --- |
| naive | 645 ms | 455 ms | −29.5% |
| naive, larger payloads | 602 ms | 428 ms | −28.8% |
| production-calibrated | 478 ms | 352 ms | −26.4% |

Stability like that is worth more than any single number's precision.

## When to stop

Stop when every independent observable is the right order of magnitude and the
delta is stable across shapes. Further tuning is fitting, and fitting is how a
benchmark starts telling you what you hoped for.
