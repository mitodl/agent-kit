# Retention: reading `ol-benchmark memory`

For when the A/B came back clean and production still disagrees. The question
stops being what a request costs and becomes what it leaves behind.

```bash
ol-benchmark memory benchmarks/<name>.toml
```

Single-arm: retention belongs to one commit, so there is nothing to check out
and nothing to compare. That also means it runs on a dirty working tree, which
the A/B refuses.

There is no per-run flag for the request count: it is `[memory] requests` in
the benchmark file, so the number a verdict was reached at is recorded beside
the verdict rather than living in someone's shell history.

## Why this is not three numbers you could gather yourself

Because the three disagree, and each alone is misleading.

**RSS** is what a container limit and an OOM killer see, and it is the number
that *cannot* tell retention from a high-water mark. CPython hands an arena
back to the operating system only when every object in it is free, so a
request that allocates a large working set and releases all of it still leaves
RSS elevated for the life of the process. Rising RSS on its own is not
evidence that anything is held.

**Live object count after a forced collection** is. Anything counted there is
reachable, not merely uncollected. Growth in this number means something holds
a reference across requests, and it is the only one of the three that
establishes that.

**Retained objects by type** turns "something is held" into a name. In
practice this is usually enough on its own: a list of per-request model
instances points at the code that made them.

## The verdicts

| Verdict | Rule | Reading |
| --- | --- | --- |
| `retaining` | Reachable objects per request ≥ `retained_objects_per_request` (default 10) | Something holds them. Find the holder |
| `high-water` | RSS per request ≥ `highwater_mib_per_request` (default 0.1 MiB), objects flat | The allocator, not a holder. Allocate less per request |
| `stable` | Neither | The process returns to where it started |

The thresholds are configuration because one project's noise is another's
leak:

```toml
[memory]
requests = 50                  # validated >= 1; warmup >= 0
retained_objects_per_request = 5.0
holders = 3                    # types to trace back to a named owner
scan_budget = 400              # gc.get_referrers calls, total
attribute = false              # growth only, no heap scans
```

Put a team-wide default in `[defaults.memory]` in the project layer; a
benchmark's own `[memory]` overrides it.

A `retaining` verdict is a prompt to look, not proof of a defect. A cache
legitimately warming over the first requests looks the same at small request
counts — raise `requests` and see whether the rate holds or decays.

## Finding the holder

`retained_by_type` names what is held. When that is not enough, the reference
graph is walked upward to the first module or class, and the chain is printed
root-first:

```
CLASS courses.models.Course
  -> dict(len=48, keys=['__module__', 'course_page', ...])
    -> lru_method_cache._MethodCacheDescriptor
      -> dict(len=16, keys=['_method', '_caches', '_locks'])
        -> dict(len=175, keys=['140615021984528', ...])
          -> list(len=1)
            -> courses.models.CourseRun
```

Read it from the top. The chain ends at something with process lifetime, and
the step below it is the container doing the holding — here a dict of 175
entries keyed by stringified `id()`, one per instance ever seen.

**`lru_caches_grown` being empty is a result.** It rules out every
`functools.lru_cache` in the process, which means the holder is hand-rolled —
and a hand-rolled cache keyed by `id(obj)` in a class-level dict will never
appear in a cache audit however carefully you do one.

## How the retained set is delimited, and the one limit

The baseline heap is frozen into the permanent generation (`gc.freeze()`)
before the requests run, so everything the collector can still enumerate
afterwards is, by construction, what the run added. Nothing is compared by
address, which is what makes the count exact rather than an estimate — and
it is why the per-request object figure is cheap enough to take on every
request without the measurement disturbing the RSS series beside it.

**The limit is that only GC-tracked containers are visible.** CPython untracks
a tuple or dict whose contents are all themselves untracked, so an object held
only in such a container has no discoverable referrers and the walk truthfully
reports nothing. A leak worth finding holds model instances, querysets or
closures, which keep their containers tracked — but a synthetic reproduction
that plants `object()` in a dict will find nothing and look like a broken
tool.

It fails toward silence rather than toward a false alarm, so a `stable`
verdict is weaker evidence than a `retaining` one.

## What retention tells you that latency did not

The two findings compose. An endpoint that loads far more rows than it
serializes is paying for it twice:

- **In latency**, once per request, for rows that are fetched, hydrated and
  discarded.
- **In memory**, permanently, *if anything retains them* — because holding one
  object from a request can pin its whole prefetch cache.

So compare `retained_by_type` against what the response actually contains. An
endpoint returning 25 rows while retaining 350 model instances per request is
not leaking 25 objects; it is leaking the over-fetch. That reframes an
over-fetch from a latency optimisation worth doing eventually into the
multiplier on a leak.

## Before believing any of it

The pass inherits the harness's refusals, and that is most of why it lives
there. Confirm in `preconditions`:

- `profilers_active` is empty. An ORM profiler allocates per fetch, and its
  objects are retained for as long as it holds them — a hand-rolled
  measurement under pytest once reported 70% more growth than the truth for
  exactly this reason, because the test environment force-enabled one.
- `under_pytest` is false. Test settings routinely enable the above.
- The response was not empty. An endpoint serving nothing has a beautifully
  flat heap.
