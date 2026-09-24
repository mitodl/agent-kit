# Writing the benchmark file

## The three layers

A benchmark is described by three files, because the three have different
owners and different lifetimes. The loader **rejects** a file that declares a
section belonging to another layer, so the separation cannot quietly drift.

| File | Committed? | Owner | Holds |
| --- | --- | --- | --- |
| `benchmarks/benchmark.toml` | yes | the project | `[django]`, `[database]`, `[defaults.measure]` |
| `benchmarks/<name>.toml` | yes | **you** | `[benchmark]`, `[knobs]`, `[seed]`, `[target]`, `[auth]`, `[measure]`, `[trace]`, `[calibration]` |
| `benchmarks/benchmark.local.toml` | **no, gitignored** | the developer | `[backend]`, and connection strings for their machine |

Precedence, lowest to highest: **project → benchmark → local → environment →
CLI flags.**

**You edit the middle one only.** If the project layer is wrong — the settings
module points at test settings, say — raise it rather than editing it; it
affects every benchmark in the repo. The local layer is someone's machine.

Every string in `[django]`, `[database]` and `[backend]` expands `${VAR}` and
`${VAR:-default}`, which is how a committed file works on a machine whose DSN
comes from a cluster. An unset variable with no default is an error naming the
variable, not an empty string.

## Knobs

Every shape dimension gets a knob. Two reasons, both load-bearing: the same
benchmark can be re-run at another shape with no edit (`--knob rows=200` or
`BENCH_ROWS=200`), and the report can state the exact shape the number was
measured at.

Annotate each one with the artifact that justifies it:

```toml
[knobs]
# OBSERVED (sample response): 100 rows per page, 13 nested per row.
rows = 100
nested_per_row = 13
# OBSERVED (trace IN-list): at least 388 child rows are loaded per page.
children = 400
# GUESS: nothing in a response or trace shows column widths. Identical in both
# arms, so it moves the baseline, not the delta.
blob_bytes = 2048
```

That comment is not decoration. The write-up has to distinguish observed from
guessed, and this is where the distinction is recorded.

## Seed steps

Steps run in declaration order and refer to each other by name.

| `kind` | Requires | Does |
| --- | --- | --- |
| `factory` (default) | `factory` | `factory-boy`, one `create()` per row |
| `model` | `model` | plain model instances; no factory-boy needed |
| `fixture` | `fixtures` | `loaddata` of committed JSON |
| `sql` | `statements` | raw SQL |
| `hook` | `hook` | calls `pkg.mod:fn(context)` |

`bulk = true` on a `factory` or `model` step builds in memory and
`bulk_create`s. Much faster for large steps, but it skips post-generation hooks
— attach many-to-many rows through the `m2m` block instead.

```toml
[[seed.step]]
name = "authors"
factory = "catalog.factories:AuthorFactory"
count = 50

[[seed.step]]
name = "books"
factory = "catalog.factories:BookFactory"
count = "$knob:rows"
kwargs = { author = "$cycle:authors", title = "Book {index}", body = "$blob:blob_bytes" }
m2m = { topics = { source = "topics", per = "$knob:nested_per_row", strategy = "cycle" } }
```

An `m2m` entry is either a `$token` resolving to a list, or a table with
`source` (an earlier step), `per` (an integer, a knob, or `"all"`) and
`strategy` (`cycle`, `random`, or `all`). `random` draws from a seeded RNG, so
the shape is identical across arms and across re-runs.

### Tokens

Resolved anywhere in `kwargs`, `m2m`, `count`, target params and auth:

| Token | Means |
| --- | --- |
| `$knob:NAME` | a shape knob |
| `$index` | the 0-based index of the row being built |
| `$blob:N` / `$blob:KNOB` | filler text of roughly N bytes |
| `$ref:STEP`, `$ref:STEP[2]` | one object from an earlier step |
| `$cycle:STEP` | that step's objects, cycled by `$index` |
| `$sample:STEP:5` | 5 of them, from the seeded RNG |
| `$all:STEP` | all of them |
| `$ids:KEY` | a scalar from `[seed.export]` — how the *request* reaches seeded rows |
| `$env:VAR` | an environment variable |
| `$$` | a literal `$` |

Plain strings also go through `str.format` with `index` and every knob, so
`"Book {index}"` works without a token.

### The escape hatch

`$cycle` spreads rows uniformly. Where production is skewed — a few parents
holding most of the children — uniform fan-out hides the prefetch cost that a
skewed one exposes. A `hook` step gets the knobs, every object built so far,
the shared RNG and the same resolver, and returns the objects it created:

```python
def skewed_books(context):
    authors = context.objects["authors"]
    return [
        BookFactory.create(author=authors[0 if i % 4 == 0 else 1 + i % (len(authors) - 1)])
        for i in range(context.knobs["books"])
    ]
```

Reach for this for a distribution, a conditional attachment, or a graph —
anything a count and a cycle cannot express.

### Getting the structure right

This is the part that decides whether the benchmark measures the endpoint or
something adjacent to it:

- **Many tenants/orgs**, so filters stay selective.
- **Children spread across parents**, not stacked on one.
- **Noise rows the page does not return**, so the main query is not scanning a
  toy table.
- **Whatever membership the permission check needs** — filters often return an
  empty queryset without it, and you will benchmark a 404 or an empty page.

Afterwards, read `m2m_pairs` in `seed.json`. Join multiplicity is invisible in
an API response, so that number is the only place the report can state it.

## Exports and the target

The request runs in a different process from the seed, so it reaches seeded
rows through exported scalars rather than objects:

```toml
[seed.export]
user_id = "user.pk"
library_id = "libraries.pk"

[target]
path = "/api/v1/libraries/"          # or reverse = "libraries:library-list"
params = { library = "$ids:library_id", page_size = 100 }
expect_status = 200
nested_keys = ["books"]

[auth]
user_id = "$ids:user_id"
```

`nested_keys` names collections whose serialized length is compared between
arms. Include them: a serializer change can keep the row count identical while
quietly dropping what is inside each row, and that is a regression wearing a
speed-up's clothes. The comparison is declared void when they differ.

Use `reverse` over `path` where the endpoint has a URL name — it fails loudly
at validation instead of returning a 404 mid-run.

## Query classifiers

These turn raw SQL into the labels in the attribution table. They are tried in
declaration order, so **most specific first** — a broad pattern placed early
swallows the ones after it:

```toml
[[trace.classify]]
label = "COUNT(*) paginator"
pattern = "COUNT\\(\\*\\)"

[[trace.classify]]
label = "** topics prefetch"        # mark what the change targets
pattern = "book_topics"

[[trace.classify]]
label = "books page query"
pattern = "FROM \"book\""
```

Mark the queries the change targets with `targeted = true`. It does two
things: makes them obvious in the output, and excludes them from the drift
check against production, which only makes sense for queries the change is
*not* supposed to move.

```toml
[[trace.classify]]
label = "topics prefetch"
pattern = "book_topics"
targeted = true
```

The `per_req` column is the check on your ordering: a non-integer value means
one label is catching two different queries and mixing their medians.

## Comparing against production

```toml
[calibration]
baseline = "<name>.baseline.json"   # relative to this file
drift_factor = 5
```

`baseline` points at a committed artifact distilled from production traces by
`ol-benchmark baseline` — per-query medians keyed by the labels above, with no
statement text in it. The raw traces are never committed; see
[evidence.md](evidence.md).

`drift_factor` is how far an **untargeted** query may sit from production
before the report calls the seed into question. Five is a reasonable default:
order-of-magnitude agreement is the bar, and chasing exactness tips into
fitting. Local is expected to be faster — no round-trip, warm cache, no
contention — so the direction that matters is local being *slower*.

## Measurement

`[defaults.measure]` in the project layer sets warm-up, iterations and traced
repeats for every benchmark. Override per benchmark only with a reason:

```toml
[measure]
iterations = 25          # this endpoint is noisy; more samples
```

`middleware_exclude` strips profiler middleware by substring, and the removal
is recorded in every result. There is no silent strip: without it, the run
refuses rather than quietly measuring something a profiler is inflating.
