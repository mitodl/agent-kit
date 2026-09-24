# Gathering the evidence

## The batched question

Ask once, with the *why* attached — people routinely have a trace or a saved
response they would not think to offer.

> To benchmark this properly I need a few things:
>
> 1. **Endpoint and exact query string**, including filter params, as the slow
>    caller sends it.
> 2. **The two refs to compare** — normally your branch and its merge base.
> 3. **A production OTel trace** of a real slow request, exported as JSON.
>    Without it I lose per-query attribution and the calibration target.
> 4. **2-3 sample production responses** for that endpoint, ideally for
>    different tenants or filter values. This is the only ground truth for the
>    response shape; app factories are usually nothing like it.
> 5. Can you run **read-only queries against a production replica?** A couple of
>    `count(*)` / `avg(length(...))` queries turn my biggest guesses into facts.

Offer the fallback explicitly: with no trace, you can still A/B, but you cannot
attribute the change to a query or calibrate the seed against a real timing.

## Reading an OTel trace export

Exports vary (`batches[].instrumentationLibrarySpans[].spans[]` for OTLP JSON,
`resourceSpans[].scopeSpans[].spans[]` for newer collectors). Normalise first:

```bash
jq -c '
[ .batches[]
  | (.resource.attributes | map({(.key): (.value|to_entries[0].value)}) | add) as $res
  | ((.instrumentationLibrarySpans // []) + (.scopeSpans // []))[]
  | .spans[]
  | { svc: $res["service.name"], name: .name,
      start: (.startTimeUnixNano|tonumber), end: (.endTimeUnixNano|tonumber),
      attrs: (.attributes // [] | map({(.key): (.value|to_entries[0].value)}) | add) }
]' trace.json > spans.json
```

### The gaps are the point

A DB span wraps `cursor.execute`. Row transfer, model instantiation and
serialization happen *after* it closes. So a fast query followed by a long gap
is the signature of over-fetching — exactly what these benchmarks chase.

```bash
jq -r '
  [ map(select(.attrs["db.statement"])) | sort_by(.start)[] ] as $db |
  ($db[0].start) as $t0 |
  range(0; ($db|length)) as $i |
  ($db[$i]) as $s |
  (if $i+1 < ($db|length) then ($db[$i+1].start - $s.end) else 0 end) as $gap |
  "@\((($s.start-$t0)/1e6)|floor)ms  sql=\((($s.end-$s.start)/1e6)*100|round/100)ms  gap=\(($gap/1e6)*10|round/10)ms  \($s.attrs["db.statement"]|gsub("\\s+";" ")|.[0:90])"
' spans.json
```

Rank by `sql + gap`. The largest few are the budget.

### Row-count floors

Prefetch queries carry `IN (%s, %s, ...)` lists. The placeholder count is a
**lower bound** on rows loaded:

```bash
jq -r '.[] | select(.attrs["db.statement"]) | .attrs["db.statement"]' spans.json \
  | grep -o '%s' | wc -l
```

Exporters usually truncate `db.statement` (commonly 2048 chars). A count taken
at the truncation point is a floor, not a count — treat it as such and say so.

### Timings to calibrate against

Note the cost of queries your change does **not** touch. Those are your
calibration targets in [calibration.md](calibration.md) precisely because they
are identical in both arms.

## Reading sample responses

Extract, per file:

```bash
jq -r '
  "rows=\(.results|length)  count=\(.count)",
  "nested per row: \([.results[].<collection>|length]|"total=\(add) min=\(min) max=\(max)")",
  "text field chars: \([.results[].<field>|length]|"mean=\((add/length)|round) max=\(max)")"
' response.json
```

What to pull out:

- **Rows per page** and whether pagination clamps a larger requested size.
- **Nested collection sizes per row** — the single most common place factories
  understate production by an order of magnitude.
- **Payload sizes of text fields** — and note that fields *selected but not
  serialized* are invisible here. They still cost query time.
- **Total response bytes**, as an end-to-end check that your seed is in range.

### Cross-file comparisons are powerful

If two responses share courses/objects and were captured close together,
differences between them cannot be data drift. That is how you distinguish
"someone edited the data" from "the code is behaving differently per request" —
a live object cannot hold two values at the same instant.

## What no artifact can tell you

Label these explicitly as guesses in the report, and note that because they are
identical across both arms they move the **baseline**, not the **delta**:

- Column payloads selected but never serialized.
- Join multiplicity on many-to-many relations.
- Rows loaded by a prefetch and then filtered out in Python.
- How rows distribute across tenants, which drives filter fan-out.

Each of these becomes a knob in `[knobs]` with a `# GUESS:` comment naming
what it stands in for. Each is also one read-only production query away from
being a fact. If the user has replica access, ask — it is worth more than any
amount of inference:

```sql
-- payload size of a column you suspect dominates a prefetch
SELECT avg(length(col)), max(length(col)) FROM tbl WHERE <active predicate>;

-- many-to-many multiplicity
SELECT count(*)::float / count(DISTINCT left_id) FROM through_table;

-- rows behind one page
SELECT count(*) FROM child WHERE parent_id IN (<the page's ids>);
```

## Landing it in the benchmark file

Everything above turns into three things in `benchmarks/<name>.toml`:

| What you extracted | Where it goes |
| --- | --- |
| Rows per page, nested sizes, payload sizes | `[knobs]`, with `# OBSERVED (sample response):` above each |
| Anything no artifact showed | `[knobs]`, with `# GUESS:` above each |
| `IN`-list placeholder counts | `[calibration.floors]`, keyed by seed step name — the seed warns when it falls short |
| Unchanged queries' production timings | `[[calibration.observable]]`, so the report carries them next to the numbers |
| The queries in your trace budget | `[[trace.classify]]`, most specific first, with the targeted ones marked `**` |

A floor recorded here keeps working after you are gone: a colleague re-running
at a smaller shape gets a warning rather than a quietly unrepresentative
number.
