# Reading the results

Everything lands in `.bench/out/<benchmark>/`.

| File | What it is for |
| --- | --- |
| `comparison.json` | **Read this first.** The verdict, the deltas, per-query attribution, the shape, the calibration table |
| `report.md` | The same, rendered for a human. Paste sections of it into the PR |
| `config.resolved.json` | Every setting in force and which layer it came from, credentials masked. This is how a number is traced back to the machine that produced it |
| `seed.json` | Per-step row counts, many-to-many pair totals, exports, floor warnings |
| `base.json`, `branch.json` | Each arm's raw timings and the conditions it ran under |
| `trace-base.json`, `trace-branch.json` | Every span: duration **and the gap to the next one** |
| `agg-base.json`, `agg-branch.json` | Median per logical query across the traced repeats |

## The verdict

`comparison.json` never reports a bare number. It reports one of three states,
with `reason` saying why.

### `void` — the arms did not do the same work

`equivalence_mismatches` lists the fields that differ: response bytes, item
count, page length, or any `nested_keys` collection. **No delta may be quoted
from a void comparison.** It is not a benchmark result, it is a bug report
about one of the two refs — usually a serializer that dropped a field, a
filter that behaves differently, or an off-by-one in pagination.

Investigate the mismatch. Do not "fix" it by removing the field from
`nested_keys`.

### `inconclusive` — the difference is inside the noise

Either the median difference is smaller than a single arm's call-to-call
variability (the gap between its best and its typical call), or min and median
disagree about which arm is faster.

This is a real answer and reporting it is correct. The honest options are: say
the change is not measurable at this shape, or raise the shape until the effect
is larger than the noise. Re-running until a number comes out the way you want
is not one of them.

### `ok` — the measurement holds

`ok` means the measurement is sound, not that the change is good. A regression
that clears the noise floor also reports `ok`, with `reason` saying "slower".
Read the sign.

## Per-query attribution

Each row of `per_query` has SQL time and gap time, separately, for both arms:

```
query                    base sql  base gap  base tot  br sql  br gap  br tot  delta
** topics prefetch          3.69     36.35     40.04    3.48    6.60   10.08  -29.96
books prefetch              1.46      4.36      5.81    1.41    4.35    5.76   -0.05
COUNT(*) paginator          1.21      0.83      2.04    1.10    0.53    1.63   -0.41
```

**The gap is usually where the time is.** A database span wraps
`cursor.execute` and nothing else, so row fetch, model instantiation and
serialization all fall between spans; the last span's gap runs to the end of
the request. A change that loads fewer rows shows up in the *gap* column, not
the SQL column. If you only look at SQL time you will conclude the queries were
already fast and the change did nothing.

Two things to check here, not just the total:

- The saving is on the query the change targets. Mark those with a `**` prefix
  in the classifier label so it is obvious.
- Nothing else moved the wrong way by a comparable amount. A win paid for by a
  regression elsewhere is not a win.

### `classifier_collisions`

A non-empty list means a `[[trace.classify]]` label matched a non-whole number
of statements per request, so it is mixing two different queries and their
medians are meaningless. Tighten the pattern, or move a more specific rule
above it — rules are tried in declaration order. Re-derive the attribution with
`ol-benchmark report` afterwards; it recomputes from the saved JSON without
re-running anything.

A whole number greater than one is fine: a prefetch that genuinely runs twice
per request has `per_req = 2.0`.

## Conditions

`preconditions`, per arm, records what the harness found and what it changed:

| Field | Read it as |
| --- | --- |
| `debug_forced_off` | `DEBUG` was on and was turned off for the run |
| `middleware_stripped` | Profiler middleware you asked to be removed, listed explicitly |
| `profilers_active` | Should be empty. Non-empty means someone set `allow_profilers` |
| `trace_function` | Should be null |
| `under_pytest` | Should be false |
| `database` | The scratch database the arm actually connected to |

Both arms should agree. If they do not, something about the environment changed
between them and the comparison is suspect even when the verdict says `ok`.

## The trace is not the headline

`trace-*.json` totals run slower than `base.json` / `branch.json`, because
instrumentation is not free and is normally inert in an app with no OTLP
endpoint. Quote wall-clock numbers from the arm files and attribution from the
aggregation. Saying "the topics prefetch went from 40 ms to 10 ms, and the
endpoint's median went from 480 ms to 352 ms" is correct; adding those two
kinds of number together is not.

`trace_warnings` tells you when no database spans were captured at all —
usually a missing psycopg instrumentation extra. That is a missing measurement,
not a zero.

## Re-reporting without re-running

```bash
ol-benchmark report benchmarks/<name>.toml
```

Recomputes `comparison.json` and `report.md` from the JSON already on disk.
Use it after fixing a classifier or adding a `[[calibration.observable]]` —
there is no reason to re-measure for either.
