# Dimensions reference

The four dimensions the [`code-review`](../SKILL.md) skill checks a diff
against, each with a real finding and a look-alike that isn't one. The
distinction in every pair is the same: a finding names a concrete failure
or cost; a non-finding is a style preference or a hypothetical that doesn't
survive the verification pass.

## Correctness

A bug: wrong output, a crash, or a concrete input that fails.

**Finding:** a function divides by `len(items)` without checking for an
empty list — `items=[]` raises `ZeroDivisionError`, and the caller three
lines up in this same diff passes a filtered list that can legitimately be
empty.

**Not a finding:** a function assumes its argument is non-negative and
isn't defensive about it, but every call site in the diff (and every
existing call site, checked) passes a value already validated upstream.
There's no concrete input that reaches this function and fails — it's a
hypothetical, not a bug in this diff.

## Simplification

Unneeded complexity: premature abstraction, dead branches, a helper that
exists for one caller.

**Finding:** a new `StrategyFactory` class with a single concrete strategy
registered and no second implementation anywhere in the codebase — the
indirection has no caller that benefits from it today.

**Not a finding:** a helper function extracted for a single caller because
the calling function was already 80 lines and the extraction makes it
readable. One caller doesn't make an extraction premature if the
alternative is a large function — the complexity metric here is
readability, not caller count.

## Efficiency

Avoidable extra work: N+1 queries, redundant recomputation, an unnecessary
full scan where an indexed lookup exists.

**Finding:** a loop that calls `User.objects.get(id=x)` once per iteration
over a list of 200 ids, where a single `User.objects.filter(id__in=ids)`
would do it in one query — confirmed by reading the loop, not assumed from
the pattern alone (some loops iterate a list already fetched in bulk one
line up).

**Not a finding:** a function recomputes a value on every call instead of
caching it, but it's called once per request and the computation is O(1) —
there's no measurable cost to point at, just a stylistic preference for
memoization.

## Reuse

Logic in this diff that duplicates something already in the repo, that
should call the existing implementation instead.

**Finding:** a new diff adds a hand-rolled retry-with-backoff loop, and
`utils/retry.py:retry_with_backoff` already implements the same thing with
jitter and a max-attempts cap this new code doesn't have — cite the
existing file:line, not just "this probably exists somewhere."

**Not a finding:** two functions in the diff both call
`.strip().lower()` on user input before comparing it — two lines of
genuinely trivial logic don't warrant extracting a shared helper; that's
premature abstraction in the other direction.
