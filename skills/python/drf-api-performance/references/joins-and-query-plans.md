# Joins and query plans reference

Detail behind the join budget in [SKILL.md](../SKILL.md), plus how to get the SQL
and the plan out of Django.

## How many joins is too many?

- Three or four to-one joins are ok (`ForeignKey` or `OneToOneField`).
- **Eight is a review threshold, not a hard ceiling.** It is where Postgres's
  default planner settings change behavior, so it is where you stop reasoning by
  join count and start reading `EXPLAIN` on production-scale data.
- Past it, cost tends to grow less predictably rather than smoothly, which is the
  real argument for splitting the query instead of widening it.

## Two ways an extra join hurts

- **Width** - each `select_related()` hop appends that table's columns to every
  row, and Django takes all of them by default. Three joins across 10-column
  tables is a 40-column row, times the page size, serialized and instantiated as
  Python objects. Degrades gradually; narrow it with
  `.only("title", "platform__name")`.
- **Multiplication** - a to-many join returns one row per combination, so 100
  courses with 20 topics each is 2,000 rows to build 100 objects, and the
  `DISTINCT` that cleans that up sorts all 2,000. Does **not** degrade gradually.
  `select_related()` won't do this, but a `filter()` across a to-many will.

## Table size enters through the plan, not the join count

| Situation | What joining a large table costs |
| --------- | -------------------------------- |
| Foreign key to primary key, indexed both sides | One index probe per output row; size shows up only as `log(n)` and cache misses |
| Join column not indexed | The planner hashes or sorts the whole table, so its size dominates |
| Hash or sort exceeds `work_mem` | It spills to disk in batches and throughput falls off a cliff |

That last row is the shape of the
[MIT Learn outage](https://engineering.ol.mit.edu/runbooks_post_mortems/20260324_mitlearn_outage/) -
in memory at RC scale, on disk at production cardinality.

Postgres also shifts planning behavior as the relation count grows. Both
thresholds below are `postgresql.conf` settings - check the effective values on
the database you actually run against (`SHOW join_collapse_limit;`) rather than
assuming the defaults:

| Setting | Default | What changes |
| ------- | ------- | ------------ |
| `join_collapse_limit` | 8 | The planner flattens explicit `JOIN` constructs into the FROM list only while that list would stay within the limit. Once it stops flattening, those joins are planned in the order written instead of being considered for reordering - so nine joins can plan worse than eight for reasons unrelated to your data. It does not stop reordering *within* what was already collapsed. |
| `from_collapse_limit` | 8 | The same, for pulling subqueries up into the parent FROM list. |
| `geqo_threshold` | 12 | At this many FROM items **or more**, planning switches from exhaustive search to a genetic algorithm, which is heuristic - the chosen plan can differ between runs of the same query. |

These count the relations in the planner's FROM list, which is not the same as
the number of `JOIN` keywords in the SQL Django emits: views and subqueries
contribute their own relations. `EXPLAIN` is the only reliable way to see which
side of a threshold you landed on.

## Evaluating more joins

> **Do not do this against live production databases.**

In between 4 and 8 joins, decide with `EXPLAIN (ANALYZE, BUFFERS)` on
production-scale data:

- **Actual rows** far above the page size means something multiplied.
- `Batches:` above 1 or `Method: external merge  Disk:` means you exceeded `work_mem`.
- **Estimates off from actual by an order of magnitude** mean the plan is
  guessing - and that error compounds with each join.

## See the queries Django is running

A count tells you how many; the SQL tells you why.

**In a test**, both `django_assert_num_queries` and
`django_assert_max_num_queries` yield the capture context, so the queries are
right there when an assertion fails:

```python
with django_assert_max_num_queries(10) as captured:
    client.get(url)

for query in captured.captured_queries:
    print(query["time"], query["sql"])
```

`CaptureQueriesContext(connection)` from `django.test.utils` is the same capture
without an assertion, for when you only want to look.

**In a shell or dev server**, point the `django.db.backends` logger at the console
to log every statement as it runs. This one does need `DEBUG = True`:

```python
LOGGING = {
    "version": 1,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "django.db.backends": {"handlers": ["console"], "level": "DEBUG"},
    },
}
```

**To read a query without running it**, `str(queryset.query)` renders the SQL -
enough to see which joins Django will emit, though parameters are interpolated
lazily and the result isn't runnable:

```python
print(Course.objects.filter(platform__name="edx").select_related("platform").query)
```

**To get the plan**, `QuerySet.explain()` passes its options through to the
database:

```python
print(
    Course.objects.filter(platform__name="edx")
    .select_related("platform")
    .explain(analyze=True, buffers=True)
)
```

> **Do not run this against production databases.** `analyze=True` really
> executes the query, which is the point - the plan without it is only an
> estimate - but it also means you pay for the query on whatever data you point
> it at.
