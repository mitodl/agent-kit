# Testing and linting reference

Detail behind the testing rules in [SKILL.md](../SKILL.md).

## Catch N+1s with zeal

Both mit-learn and mitxonline run
[django-zeal](https://pypi.org/project/django-zeal/) over their test suites. If
your project is not set up with this, you should configure it. Zeal instruments
the ORM and reports when the same relation is queried more than once, naming the
line responsible:

```text
N+1 detected on courses.Course.runs at courses/models.py:25 in get_runs
```

Zeal also flags N+1s caused by `.only()`, `.defer()`, and `.get()`, not just
missing prefetches.

## Give N+1 checks enough data

A detector that never sees a query repeat has nothing to report, so the checks are
only as good as the data the test creates.

For a list API, create **5-10 records at each level** of the response. Taking
`/api/courses/?id=234,62`, that means creating several courses and then several
topics per course. Zeal reports on `ZEAL_NPLUSONE_THRESHOLD`, which defaults to
the same query twice, so a single related record can never trip it - one course
with one topic is indistinguishable from a properly prefetched endpoint.

## `skip_nplusone_check` is for tech debt only

Both repos register a marker that turns zeal off for a single test, through an
autouse fixture in `fixtures/common.py`:

```python
@pytest.fixture(autouse=True)
def check_nplusone(request):
    """Raise nplusone errors"""
    if request.node.get_closest_marker("skip_nplusone_check"):
        with zeal_ignore():
            yield
    else:
        yield
```

`zeal_ignore()` with no arguments suppresses **every** check for the duration of
the test, so `@pytest.mark.skip_nplusone_check` is a blanket exemption. It exists
so that known, pre-existing N+1s don't block unrelated work.

> **Don't add the marker to a new test.** On new code the marker isn't recording
> tech debt, it's hiding a bug before it ships - along with every other N+1 in
> that test. Add the prefetch instead.

For a genuine false positive, scope the exemption instead of the whole test:
`zeal_ignore([{"model": "polls.Question", "field": "options"}])` silences exactly
one relation, and `ZEAL_ALLOWLIST` does the same globally. Removing a marker is
also a legitimate piece of work in its own right - the count only goes down if
somebody drives it down.

## Assert query counts directly

Zeal answers "does anything repeat here?" It won't notice a view going from 4
queries to 14 as long as none of them repeat. pytest-django provides two fixtures
for that:

| Fixture | Asserts | Reach for it when |
| ------- | ------- | ----------------- |
| `django_assert_num_queries(n)` | exactly `n` queries | you want the count pinned |
| `django_assert_max_num_queries(n)` | at most `n` queries | you want a budget and the exact number is noisy |

The assertion that actually pins down an N+1 is a **constant count over a varying
amount of data**. Parametrize the cardinality and keep the expected number fixed -
mit-learn's channel tests are the pattern to copy:

```python
@pytest.mark.parametrize("related_count", [1, 5, 10])
def test_no_excess_by_type_name_detail_queries(
    client, django_assert_num_queries, related_count
):
    """By-type detail query count should remain constant."""
    expected_query_count = 4

    channel = ChannelFactory.create(is_topic=True)
    ChannelListFactory.create_batch(related_count, channel=channel)
    SubChannelFactory.create_batch(related_count, parent_channel=channel)

    url = reverse(
        "channels:v0:channel_by_type_name_api-detail",
        kwargs={"channel_type": ChannelType.topic.name, "name": channel.name},
    )

    with django_assert_num_queries(expected_query_count):
        response = client.get(url)
```

Passing at `related_count=10` with the same number as at `1` is the property you
care about - the endpoint is flat in the number of children - stated as an
assertion rather than inferred.

- A failure prints the queries it captured, which is usually enough to spot the
  relation that was missed. Both fixtures also yield the context, so
  `context.captured_queries` is there for a closer look - see
  [joins-and-query-plans.md](joins-and-query-plans.md#see-the-queries-django-is-running).
- **Use both.** mit-learn pins
  `django_assert_num_queries(21)  # should be same # regardless of child count`
  on program detail, and gives user lists a
  `django_assert_max_num_queries(query_budget)` ceiling instead.
- **Expect to update the numbers.** A legitimate change to a view moves the count,
  and that diff line is the prompt to check the new number is still constant in
  cardinality.

## Lint serializers with drf-lint

Runtime N+1 checks only fire on code paths your tests actually exercise.
[mitol-drf-lint](https://github.com/mitodl/ol-django/tree/main/src/drf_lint)
closes the gap statically: it parses serializer modules with
[LibCST](https://libcst.readthedocs.io/) and flags ORM calls made inside
serializer methods, which is where N+1s come from.

| Rule | Flags |
| ---- | ----- |
| `ORM001` | Manager access inside a serializer method - `Course.objects.filter(...)` |
| `ORM002` | Related-manager queryset call inside a serializer method - `instance.topics.all()`, `instance.children.order_by(...).first()` |

Both mitxonline and mit-learn run it as a local pre-commit hook over
`serializers.py`:

```yaml
- repo: local
  hooks:
    - id: drf-serializer-orm-check
      name: DRF Serializer ORM Check
      description: "Detects Django ORM queries inside DRF serializer methods (N+1 risk)"
      entry: drf-lint
      args: [--baseline, drf_lint_baseline.json]
      language: python
      files: 'serializers\.py$'
      additional_dependencies:
        - mitol-drf-lint
```

`drf_lint_baseline.json` records the violations that already existed when the hook
was adopted, so the build only fails on new ones. Regenerate it with
`drf-lint --generate-baseline --baseline drf_lint_baseline.json <paths>`. A single
line can be suppressed with `# noqa: ORM001` / `# noqa: ORM002`.

> **A baseline entry is a known bug, not a decision.** Growing the baseline is how
> we increase technical debt. Fix the serializer with a prefetch instead, and let
> the baseline shrink over time.

The linter also can't tell whether a prefetch is in place - it only sees the query
in the serializer. Keep `required_prefetches` and the runtime N+1 checks doing
that half of the job.
