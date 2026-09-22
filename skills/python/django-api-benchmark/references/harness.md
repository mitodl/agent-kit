# Building the harness

## Why not pytest

Test settings are tuned for catching bugs, not for timing. Three problems, in
order of severity:

**N+1 profilers.** Projects commonly enable something like `django-zeal` under
the test environment. Its middleware hooks every ORM fetch and walks the Python
stack on each one. That cost is proportional to the number of objects
hydrated — which is usually the exact variable a prefetch change moves. It
inflates whichever arm loads more rows and **overstates the improvement**.
Per-test opt-out markers often only suppress the *report*, leaving the
instrumentation in place, so check what the escape hatch actually does before
trusting it.

**Coverage.** Frequently in `addopts`, often with branch tracing. It dwarfs
whatever you are measuring.

**Autouse fixtures.** A dozen patches, a settings snapshot, and DB
setup/teardown per test.

Test-environment flags are also commonly forced by `pytest-env`, which
overrides the process environment — so you cannot always switch the profiler
off from the shell.

Running through `manage.py shell` with the app's normal dev settings avoids all
of it: the profiler is typically not installed at all, and there is no
coverage, no xdist, no fixtures.

## A throwaway database

Django reads `DATABASE_URL` in most mitodl apps (via `dj_database_url`).
Point it at a scratch database:

```bash
DB_URL="postgres://<user>:<pass>@<host>:5432/bench_db"
docker compose exec -T db psql -U postgres -q \
  -c "DROP DATABASE IF EXISTS bench_db" -c "CREATE DATABASE bench_db"
docker compose run --rm --no-deps -e DATABASE_URL="$DB_URL" -e DEBUG=False \
  web python manage.py migrate --no-input
```

Two reasons this beats seeding the dev database:

- The dev database is untouched.
- The data is seeded **once** and survives the `git switch`, so both arms see
  byte-identical rows. No reseeding, no factory-determinism problem, and the
  two response bodies are directly comparable.

### Environment overrides you will likely need

| Variable | Why |
| --- | --- |
| `DATABASE_URL` | The scratch database |
| `DEBUG=False` | Dev often defaults to `True`; `DEBUG` makes Django record every query |
| Object-storage off (e.g. `*_USE_S3=False`) | Image/file factories otherwise try to talk to S3 and fail |

If the seed blows up in a storage backend, that is the storage flag.

## Switching refs

`docker compose` bind-mounts the repo into the app container, so a host-side
`git switch` changes what the container runs. Keep the harness in an
**untracked** `.bench/` directory — untracked files survive branch switches:

```bash
mkdir -p .bench && echo '.bench/' >> .git/info/exclude
```

Check the working tree is clean first, and use a `trap` so an interrupted run
still returns to the original ref.

## Measuring

**Warm up and discard.** The first requests pay content-type caches, CMS
site-root caches, connection setup and first-call imports. Three warm-ups, then
10-15 measured iterations.

**Report min and median.** Min is the least noise-contaminated estimate of the
true cost; median guards against a single fast outlier. Report max too — if it
is multiples of the median, the machine is noisy and you should say so.

**Separate the query-capture pass.** `CaptureQueriesContext` sets
`force_debug_cursor`, which times every `execute` and retains every SQL string.
That cost grows with statement size, so it must not contaminate wall-clock. Run
it once, separately, for query count and summed SQL time.

**Derive Python time as `total − sum(sql)`.** That is the row-fetch and
model-instantiation cost, which is what over-fetching changes.

**Assert preconditions and exit.** The harness should refuse to run rather than
produce a wrong number:

```python
if [m for m in settings.MIDDLEWARE if "zeal" in m]:
    raise SystemExit("profiler active; it would overstate the win")
if settings.DEBUG:
    raise SystemExit("DEBUG on; Django would record every query")
```

**Record equivalence fields** every run: response byte length, item counts,
nested item counts. If they differ between arms the comparison is void.

## Tracing for attribution

The app's telemetry is usually disabled without an OTLP endpoint, which is what
makes the timing runs clean. For attribution, stand up your own provider with an
in-memory exporter *inside* the bench process — no collector, no network.

Order matters: instrument **before** building the test client (the Django
instrumentor inserts middleware, and a handler caches its middleware chain) and
close the DB connection afterwards so the instrumented `connect` is used.

Capture 5-7 requests and take the **median of each query position**. One traced
request is far too noisy to attribute a gap to a query.

Instrumentation is not free: traced totals run differently from the
uninstrumented timing run. **The trace is for attribution; the uninstrumented
run is for the number.** Say that whenever you show both.

## Env-var contract

Keep every shape knob an environment variable so re-running at a different
shape needs no edit — and so the report can state the exact shape:

| Variable | Meaning |
| --- | --- |
| `BENCH_IDS_PATH` | Where `seed.py` writes ids for `bench.py`/`trace.py` |
| `BENCH_LABEL` | Arm name, used in output filenames |
| `BENCH_ITERATIONS`, `BENCH_WARMUP` | Measurement loop |
| `BENCH_TRACE_REPEATS` | Traced requests to median over |
| `BENCH_<SHAPE_KNOB>` | One per seed dimension — row counts, fan-out, payload sizes |

`seed.py` should print its resulting shape as one machine-readable line and
**warn when it falls below a floor derived from the trace**.
