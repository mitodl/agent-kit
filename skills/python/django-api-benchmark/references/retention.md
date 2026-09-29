# Retention: reading `ol-benchmark memory`

For when the A/B came back clean and production still disagrees. The question
stops being what a request costs and becomes what it leaves behind.

```bash
ol-benchmark memory benchmarks/<name>.toml
```

Single-arm: retention belongs to one commit, so there is nothing to check out
and nothing to compare. That also means it runs on a dirty working tree, which
the A/B refuses — and that the commit it reports is where the measurement
started rather than necessarily what it ran. `ref` comes back
`git describe --dirty` style with `dirty_tree` beside it, so a result measured
against uncommitted changes cannot be quoted against a bare hash by accident.
The A/B never has to say this, because it will not start on a dirty tree.

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
| `high-water` | RSS per request ≥ `highwater_mib_per_request` (default 0.1 MiB), objects below their threshold | Consistent with the allocator rather than a holder. Allocate less per request |
| `stable` | Neither rate was crossed | Nothing crossed the thresholds — which is not the same as nothing being held |

Each row is a threshold rule and nothing more. `stable` means both rates came
in under what the config set, so growth beneath them reads identically to no
growth at all. `high-water` means the tracked-object rate stayed under its
threshold while RSS did not, which the allocator explains — but so does
retention the collector cannot enumerate: an untracked container, or anything
held by C code. Check that the RSS series plateaus before spending a day on
allocation work.

The thresholds are configuration because one project's noise is another's
leak:

```toml
[memory]
requests = 50                  # validated >= 1; warmup >= 0
retained_objects_per_request = 5.0
holders = 3                    # types to trace back to a named owner
scan_budget = 400              # gc.get_referrers calls, total
attribute = false              # skips attribution, not the growth count
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

**`lru_caches_grown` being empty is a result.** No `functools.lru_cache` in
the process gained entries, which points at a hand-rolled holder — and a
hand-rolled cache keyed by `id(obj)` in a class-level dict will never appear
in a cache audit however carefully you do one. It does not clear the category,
though: the comparison is on each wrapper's `cache_info().currsize`, so a
cache that gains no entries while the values already in it accumulate
references is flat by this measure.

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

## Why the number is not simply what the process kept

The instrument retains more per request than the threshold allows.
`Client.request` re-connects `template_rendered` and `got_request_exception`
on every call and its handler re-connects `request_started` and
`request_finished`; `Signal.connect` registers a `weakref.finalize` against
the owner of each receiver, and two of those owners never die —
`close_old_connections` is a module-level function and `store_exc_info` is
bound to the long-lived client. So twelve objects a request accumulate in
`weakref.finalize._registry` before the endpoint does anything, which is above
the default threshold on its own: unaddressed, a view returning a fixed string
reads `retaining` and the command exits 1.

Each of those finalizers is detached as it appears, matched both on its
callback and on being held against something only a test client can own. That
second half matters: an application that calls `connect` on every request with
a receiver that outlives it leaks a finalizer a request *in production too*,
and that is the finding rather than the instrument. It stays visible.

`harness_finalizers_detached` reports the count — three per request on
current Django. `null` means the pass could not identify them on the Django in
use and the figures include the instrument; read a `retaining` verdict with
that in mind and compare against a trivial endpoint before believing it.

This is the concrete reason not to hand-roll the measurement. A script that
loops over an endpoint counting live objects is, before anything else,
measuring its own test client. Subtracting an estimate does not fix it either:
the obvious estimate — the same request against a path the URL resolver
rejects — measures 44 objects a request rather than twelve in `ol-django`,
because a 404 logs a warning and a log handler holds the record and the
`WSGIRequest` it pins. Subtracting that would hide a leak of thirty objects a
request.

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
there. Two of them are environment facts to confirm in `preconditions`, which
carries environment fields and nothing else:

- `profilers_active` is empty. An ORM profiler allocates per fetch, and its
  objects are retained for as long as it holds them — a hand-rolled
  measurement under pytest once reported 70% more growth than the truth for
  exactly this reason, because the test environment force-enabled one.
- `under_pytest` is false. Test settings routinely enable the above.

The third and fourth are not fields but refusals, and they raise before
anything is measured. An empty response, because an endpoint serving nothing
has a beautifully flat heap: `[target].allow_empty` opts back in, and a run
that sets it is reporting on a request that returns no rows, which is worth
saying out loud in the write-up. And a committed `benchmark.local.toml`,
because that is one developer's cluster and connection strings sitting in the
repository — single-arm excuses the dirty-tree check and nothing else.

One thing is reported rather than refused. `harness_finalizers_detached`
being `null` means the test client's own retention is still inside the
figures — see "Why the number is not simply what the process kept" above.
