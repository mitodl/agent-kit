---
name: django-api-benchmark
description: >
  Prove whether a Django/DRF performance change actually made an endpoint faster,
  by A/B-ing two git refs against one production-shaped local database. Use this
  skill when asked to benchmark an endpoint, measure or verify a performance fix,
  answer "is this actually faster", size a speedup before merging, or turn a
  production OTel trace into a reproducible local experiment. Covers eliciting
  production evidence (traces, sample API responses), calibrating a seed to the
  real data shape, generating the harness, and reporting a defensible number.
license: BSD-3-Clause
metadata:
  category: python
---

# Benchmarking a Django API change

A performance PR that says "this should be faster" is a guess. This skill turns
it into a measurement: seed a **production-shaped** throwaway database, run the
same request on both git refs against **identical rows**, and attribute the
difference query by query.

Two failure modes make a benchmark worse than none, and most of this skill
exists to avoid them:

1. **Measuring the harness.** Instrumentation that scales with the thing you
   changed will manufacture a result. See [Pitfalls](#pitfalls).
2. **Measuring the wrong shape.** Factory defaults are nothing like production.
   A seed that is off structurally produces a number with no bearing on the
   endpoint you care about.

Related: [`drf-api-performance`](../drf-api-performance/SKILL.md) is about
*writing* fast endpoints. This is about *proving* one got faster.

## Step 1 — Ask for the evidence

Do not start seeding. Ask for all of it in **one batched question**, and say
what each artifact is for — people usually have more than they volunteer:

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

## Step 3 — Build the harness

An untracked `.bench/` directory (add it to `.git/info/exclude`).

First **install an execution backend**. The harness has to run `manage.py`, reach
Postgres, and make a `git switch` take effect in whatever is actually executing
the app — the only three things that differ between dev environments. They live
behind `.bench/backend.sh`; copy
[`backends/compose.sh`](references/backends/compose.sh) (a per-repo
`docker compose` stack) or [`backends/k8s-tilt.sh`](references/backends/k8s-tilt.sh)
(ol-infrastructure's k3d + Tilt `local-dev`) there. Both may be present on one
machine — **ask rather than guess**. See
[references/environments.md](references/environments.md).

Then adapt the templates:

| Template | Role |
| --- | --- |
| [`templates/seed.py`](references/templates/seed.py) | Build the dataset; every shape knob an env var; print the shape |
| [`templates/bench.py`](references/templates/bench.py) | Wall-clock A/B; asserts its own preconditions |
| [`templates/trace.py`](references/templates/trace.py) | In-process OTel capture for per-query attribution |
| [`templates/agg.jq`](references/templates/agg.jq) | Median-per-query aggregation of the traced repeats |
| [`templates/run.sh`](references/templates/run.sh) | Recreate DB → migrate → seed → arm A → switch ref → arm B |
| [`backends/`](references/backends/) | The one file that knows what your dev environment is |

Non-negotiables, each of which exists because skipping it produces a wrong
number — details in [references/harness.md](references/harness.md):

- **Run through `manage.py shell`, not pytest.** Test settings commonly enable
  profilers and coverage that scale with the work under test.
- **Point `DATABASE_URL` at a dedicated throwaway database.** Never the dev one,
  and never a name that does not start with `bench` — this step drops it.
- **Seed once; switch git refs around the seeded database.** Reseeding per arm
  reintroduces data variance and destroys comparability.
- **Verify each arm ran the ref you think, rather than assuming `git switch`
  took effect.** It is immediate under a bind mount and asynchronous under a
  push-based sync.
- **Assert your preconditions and exit if they fail** — profiler off, `DEBUG`
  off. A benchmark that silently measures the wrong thing is the whole risk.
- **Separate the wall-clock pass from the query-capture pass.** Capturing
  queries forces a debug cursor whose cost grows with statement size.
- **Warm up, discard, then report min and median** over 10-15 iterations.

## Step 4 — Calibrate, and be willing to falsify

Tune the seed until the **independent observables** match production. Independent
means "not affected by the change under test" — those are legitimate calibration
targets precisely because they are identical in both arms.

Build a table and keep it in the final report:

| observable | source | production | seed |
| --- | --- | --- | --- |
| rows per page | response | | |
| nested collection sizes | response | | |
| response body size | response | | |
| row-count floors | trace `IN` lists | | |
| cost of an *unchanged* query | trace | | |

**A seed parameter that makes an unchanged query wildly slower than production
is falsified — discard it, however good the story was.** Structural realism
matters more than sizing: how rows fan out across joins dominates how wide they
are. See [references/calibration.md](references/calibration.md) for the worked
example and the common structural mistakes.

## Step 5 — Run it, and check the benchmark before the result

Before quoting any delta:

1. **Both arms returned the same response** — byte length and item counts. If
   not, the comparison is void; find out why.
2. **Query counts differ only as expected**, confirming each arm ran the code
   you think it did.
3. **The delta exceeds the run-to-run spread.** If min and median overlap
   between arms, report *inconclusive* rather than a number.
4. **Re-run at a second seed shape.** A delta stable across shapes is the
   single strongest evidence you can produce locally.
5. **Each arm reports the ref it actually ran**, and the two differ.

## Step 6 — Report a floor, not an estimate

Local Postgres has no network round-trip, a warm cache and no contention.
Production has all three, and they penalise larger result sets
disproportionately. Say so, every time.

State in the write-up:
- The seed shape, next to the numbers.
- The execution environment, and anything it contributes to the spread —
  a benchmark sharing a pod with a live server is noisier than one in an
  isolated container, and a shared database has contention a dedicated one
  does not.
- Which inputs were **guesses**, and that they are identical across arms (so
  they move the baseline, not the delta).
- Any production signal the harness **failed** to reproduce, and what you ruled
  out.
- Per-query attribution, so a reviewer can see the saving is where the change
  aims and nothing regressed.

Do not quote a single headline number without the shape it was measured on.

## Pitfalls

| Pitfall | Why it ruins the result |
| --- | --- |
| Benchmarking under pytest | N+1 profilers (e.g. django-zeal) hook every ORM fetch; that cost scales with objects hydrated, inflating whichever arm loads more and **overstating the win** |
| Coverage left on | Line/branch tracing dwarfs the effect |
| `DEBUG=True` | Django records every query; cost grows with statement size |
| Reseeding between arms | Different rows, so it is not an A/B |
| One traced request | Per-query gaps are far too noisy; take a median of 5-7 |
| Quoting traced totals as the result | Instrumentation is not free; the trace is for attribution, the uninstrumented run is for the number |
| Factory defaults as "realistic" | Blank rich-text fields, one related row where production has dozens |
| Tuning the seed against the query you changed | Circular. Calibrate only on unchanged observables |
| Extrapolating local ms to production ms | Different hardware, cache state and network. Report a floor |
| Measuring inside a pod running an auto-reloader | The reloader watches the source tree, so switching refs — or writing harness output there — re-imports the app while you are timing it |
| Letting the destructive step inherit the ambient cluster context | A developer's current `kubectl` context is routinely a deployed environment; this harness runs `DROP DATABASE` |

## References

| Read this | For |
| --- | --- |
| [evidence.md](references/evidence.md) | The batched question to ask, reading an OTel JSON export, extracting shape from sample responses, what no artifact can tell you |
| [harness.md](references/harness.md) | Why `manage.py shell` over pytest, the throwaway-DB and ref-switching mechanics, measurement method, the env-var contract |
| [environments.md](references/environments.md) | The backend contract, `docker compose` vs local-dev k8s, why a ref switch needs waiting for, fidelity differences to disclose, the `DROP DATABASE` guards |
| [calibration.md](references/calibration.md) | The observables table, structural vs sizing realism, a worked falsification, auditing factory defaults |
| [templates/](references/templates/) | `seed.py`, `bench.py`, `trace.py`, `run.sh` |
| [backends/](references/backends/) | `compose.sh`, `k8s-tilt.sh` |

## Resources

- [Write Performant APIs](https://engineering.ol.mit.edu/handbook/how-to/write-performant-apis/)
- [OpenTelemetry Python: in-memory span exporter](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.export.html)
- [Django: `CaptureQueriesContext`](https://docs.djangoproject.com/en/stable/topics/testing/tools/#django.test.utils.CaptureQueriesContext)
- [Django: `DATABASES`](https://docs.djangoproject.com/en/stable/ref/settings/#databases)
