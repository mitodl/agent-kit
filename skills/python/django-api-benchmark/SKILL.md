---
name: django-api-benchmark
description: >
  Prove whether a Django/DRF performance change actually made an endpoint faster,
  by A/B-ing two git refs against one production-shaped local database. Use this
  skill when asked to benchmark an endpoint, measure or verify a performance fix,
  answer "is this actually faster", size a speedup before merging, or turn a
  production OTel trace into a reproducible local experiment. Covers eliciting
  production evidence (traces, sample API responses), calibrating a seed to the
  real data shape, writing the benchmark's config file, and reporting a
  defensible number using the mitol-django-benchmark package.
license: BSD-3-Clause
metadata:
  category: python
---

# Benchmarking a Django API change

A performance PR that says "this should be faster" is a guess. This skill turns
it into a measurement: seed a **production-shaped** throwaway database, run the
same request on both git refs against **identical rows**, and attribute the
difference query by query.

The mechanics are handled by
[`mitol-django-benchmark`](https://github.com/mitodl/ol-django/tree/main/src/benchmark).
**You do not write a harness.** You write one TOML file describing the shape and
the target, run one command, and read the JSON it produces. Everything the
package enforces — refusing to measure under a profiler, seeding once across
both arms, verifying each arm ran the ref you think — is covered by its own
tests, so it is not your job to re-derive.

What is left is the part no package can do for you, and it is the part that
decides whether the number means anything:

1. **Getting the shape right.** Factory defaults are nothing like production. A
   seed that is off structurally produces a number with no bearing on the
   endpoint you care about.
2. **Not fitting the seed to the answer you want.** Calibrating against the
   query you changed is circular, and it will manufacture a confident,
   large, wrong result.

Related: [`drf-api-performance`](../drf-api-performance/SKILL.md) is about
*writing* fast endpoints. This is about *proving* one got faster.

## Step 0 — Install it

```bash
uv add --dev "mitol-django-benchmark[drf,factories,django,postgres]"
```

Use the `postgres2` extra instead of `postgres` on a project still using
psycopg2. Without a psycopg instrumentation extra the run still works, but the
trace pass reports that it captured no database spans and you lose per-query
attribution.

If the project has no `benchmarks/` directory yet:

```bash
ol-benchmark init --project     # benchmarks/benchmark.toml, committed
ol-benchmark init --local       # benchmarks/benchmark.local.toml, gitignored
```

## Step 1 — Ask for the evidence

Do not start writing the seed. Ask for all of it in **one batched question**,
and say what each artifact is for — people usually have more than they
volunteer:

| Ask for | Why you need it |
| --- | --- |
| The **endpoint and exact query string**, including filter params | Filters drive join fan-out; the wrong params benchmark a different code path |
| The **two git refs** to compare (usually the branch and its merge base) | The A/B arms |
| A **production OTel trace** of a real slow request (JSON export) | The only source of per-query timings and the row-count floor |
| **Sample production API responses** for that endpoint (2-3, different tenants/filters) | The only ground truth for the *output* shape |
| Whether they can run **read-only queries against a production replica** | Collapses the biggest guesses into facts |
| Anything they already know is slow | Their intuition is usually a good prior |

If they have no trace, the benchmark can still run, but say plainly that you
lose per-query attribution and the calibration target.

### Step 1b — Collect what they said they have

The answer to Step 1 tells you which artifacts **exist**. It does not hand you
any of them. Every artifact they said yes to has to arrive before Step 2, and
asking for it is a separate turn:

- For **each** trace or sample response they claimed, ask for it by name — a
  file path you can read, or pasted content. Anywhere you can read it is fine —
  offer a scratch path outside the repo so production data is never at risk of
  being committed.
- **Do not infer a path.** A trace you guessed at is worse than no trace,
  because the resulting knobs get labelled OBSERVED.
- For **replica access**, hand them the exact read-only SQL from
  [references/evidence.md](references/evidence.md) rather than asking them to
  invent queries.
- Confirm each one landed: read the file and state what you got from it (rows
  per page, span count) before moving on.

**You may not enter Step 2 with a promised artifact still outstanding.** Either
it is in hand, or they explicitly dropped it — and if they dropped it, every
knob it would have justified is a `# GUESS:`, not an `# OBSERVED:`. Offering to
"proceed with estimates and swap them in later" re-labels guesses as facts the
moment the report is written; ask again instead.

## Step 2 — Derive the target shape from the artifacts

Each artifact answers different questions. Be explicit about which, because
anything neither can answer is a **guess you must label as one**.

- **Sample responses** give you the output shape exactly: rows per page, nested
  collection sizes per row, field payload sizes. Compare these against what the
  app's factories actually produce — the gap is usually large in both directions.
- **The OTel trace** gives you the query timeline. Compute each span's duration
  *and the gap to the next span*: the span wraps `cursor.execute`, so row
  fetch, model instantiation and serialization all land in the gaps. It also
  gives **row-count floors** from `IN (%s, %s, ...)` placeholder counts, and
  per-query timings you can calibrate against.
- **Neither** can show you hidden fan-out: rows loaded and discarded, join
  multiplicity, or column payloads that are selected but never serialized.

See [references/evidence.md](references/evidence.md) for the jq to turn a trace
export into a gap-annotated timeline, and what to extract from responses.

## Step 3 — Write the benchmark file

```bash
ol-benchmark init --benchmark <name>     # benchmarks/<name>.toml
```

**This is the only file you edit.** `benchmark.toml` is a project-wide decision
and `benchmark.local.toml` belongs to whoever owns the machine; if either needs
changing, say so and ask.

What goes in it, and the full token vocabulary, is in
[references/configuring.md](references/configuring.md). The three things that
decide whether the result is worth anything:

- **Every shape dimension is a knob**, so the same benchmark re-runs at another
  shape with `--knob` and no edit, and the report can state the exact shape.
- **Each knob is annotated OBSERVED or GUESS**, in a comment naming the
  artifact that justifies it. This distinction has to survive into the report.
- **Structure before sizing.** Many tenants, children spread across parents,
  noise rows the page does not return, and whatever membership the permission
  check needs. Getting this wrong is not recoverable by tuning payload sizes.

Then check it loads before running anything destructive:

```bash
ol-benchmark validate benchmarks/<name>.toml
```

## Step 4 — Calibrate, and be willing to falsify

Tune the seed until the **independent observables** match production.
Independent means "not affected by the change under test" — those are
legitimate calibration targets precisely because they are identical in both
arms.

Record them in the file, so the report cites them automatically:

```toml
[[calibration.observable]]
name = "rows per page"
source = "sample response"
production = 100

[calibration.floors]
children = 388          # from the trace's IN-list placeholder count
```

**A seed parameter that makes an *unchanged* query wildly slower than
production is falsified — discard it, however good the story was.** Structural
realism matters more than sizing: how rows fan out across joins dominates how
wide they are. See [references/calibration.md](references/calibration.md) for
the worked example and the common structural mistakes.

## Step 5 — Run it, and check the benchmark before the result

```bash
ol-benchmark run benchmarks/<name>.toml --base-ref <merge-base>
```

The working tree must be clean — the runner refuses otherwise, because
otherwise the two arms are not the two refs you think.

Read `.bench/out/<name>/comparison.json`. Before quoting any delta:

1. **`verdict` is not `void`.** Void means the arms returned different
   responses; `equivalence_mismatches` says which fields. They did not do the
   same work. Find out why before anything else.
2. **`verdict` is not `inconclusive`.** That is a real answer — report it as
   one. Do not re-run until you get a number you like.
3. **`classifier_collisions` is empty.** A non-empty entry means a
   `[[trace.classify]]` label is mixing two different queries, so its median is
   meaningless. Tighten the pattern.
4. **`per_query` shows the saving where the change aims**, and nothing else
   regressed to pay for it.
5. **`refs.base` and `refs.branch` differ.** Identical refs means the switch
   never reached the code being measured.
6. **Re-run at a second shape** (`--knob rows=200`). A delta stable across
   shapes is the single strongest evidence you can produce locally.

[references/results.md](references/results.md) is the field-by-field guide,
including how to read the SQL-versus-gap split.

## Step 6 — Report a floor, not an estimate

Local Postgres has no network round-trip, a warm cache and no contention.
Production has all three, and they penalise larger result sets
disproportionately. Say so, every time — `comparison.json` carries the sentence
in `caveat`; do not drop it.

State in the write-up:

- The seed shape, next to the numbers.
- The execution environment, and anything it contributes to the spread — a
  benchmark sharing a pod with a live server is noisier than one in an isolated
  container, and a shared database has contention a dedicated one does not.
  See [references/environments.md](references/environments.md).
- Which inputs were **guesses**, and that they are identical across arms (so
  they move the baseline, not the delta).
- Any production signal the harness **failed** to reproduce, and what you ruled
  out.
- Per-query attribution, so a reviewer can see the saving is where the change
  aims and nothing regressed.

Do not quote a single headline number without the shape it was measured on.

## What the package already refuses

You do not need to guard against these; it will not run. Knowing *why* still
matters, because the reasons shape how you read a result:

| Refused | Why it would ruin the number |
| --- | --- |
| N+1 profiler middleware (`zeal`, `nplusone`, `silk`, debug toolbar) | Hooks every ORM fetch, so the cost scales with objects hydrated — the exact variable a prefetch change moves. Inflates whichever arm loads more rows and **overstates the win** |
| A trace function installed (coverage, a profiler, a debugger) | Line tracing dwarfs the effect |
| Running under pytest | Test settings commonly enable both of the above |
| A dirty working tree | The arms would not be two refs |
| A scratch database not named `bench*` | The step drops it, and a shared cluster has real databases next to it |
| A `benchmark.local.toml` that is tracked by git | It holds one developer's cluster and credentials |

It also forces `DEBUG = False` (and records that it did), seeds once across both
arms, keeps the query-capture pass separate from the timed loop, and verifies
each arm is running the ref you think before measuring it.

## Pitfalls it cannot catch

These are judgment, which is why they are the skill's job:

| Pitfall | Why it ruins the result |
| --- | --- |
| Tuning the seed against the query you changed | Circular. Calibrate only on unchanged observables |
| Factory defaults as "realistic" | Blank rich-text fields, one related row where production has dozens |
| Every row under one tenant | Makes the filter match everything and turns an unchanged query pathological |
| Quoting a traced total as the result | Instrumentation is not free. The trace is for attribution; the uninstrumented pass is for the number |
| Extrapolating local ms to production ms | Different hardware, cache state and network. Report a floor |
| Quoting a delta from one seed shape | A result that does not hold at a second shape is a result about your seed |

## References

| Read this | For |
| --- | --- |
| [evidence.md](references/evidence.md) | The batched question, collecting the artifacts they said they have, reading an OTel JSON export, extracting shape from sample responses, what no artifact can tell you |
| [configuring.md](references/configuring.md) | The three config layers, seed step kinds, the `$token` vocabulary, targets, auth, query classifiers |
| [calibration.md](references/calibration.md) | The observables table, structural vs sizing realism, a worked falsification, auditing factory defaults |
| [results.md](references/results.md) | Every output file, the verdicts, SQL versus gap, what a classifier collision means |
| [environments.md](references/environments.md) | Choosing a backend, the local-dev cluster's namespaces and DSN, fidelity differences to disclose |

## Resources

- [`mitol-django-benchmark`](https://github.com/mitodl/ol-django/tree/main/src/benchmark) — the package, its README and its own `AGENTS.md`
- [Write Performant APIs](https://engineering.ol.mit.edu/handbook/how-to/write-performant-apis/)
- [OpenTelemetry Python: in-memory span exporter](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.export.html)
