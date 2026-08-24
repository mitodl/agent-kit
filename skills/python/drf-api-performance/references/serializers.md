# Serializer reference

Detail behind the serializer rules in [SKILL.md](../SKILL.md).

## Keep queries out of serializers

A serializer body runs **once per object**. Any query inside it is therefore
multiplied by the page size, and a query in a nested serializer is multiplied
again by the number of children. This is where essentially every N+1 in our APIs
comes from.

The view assembles the data; the serializer only formats what is already in
memory. A `SerializerMethodField` that touches the ORM has taken on the view's job:

```python
# Don't - one COUNT query per course in the response
class CourseSerializer(serializers.ModelSerializer):
    topic_count = serializers.SerializerMethodField()

    def get_topic_count(self, instance):
        return instance.topics.count()
```

```python
# Do - one query for the whole page, computed by the database
# views.py
queryset = Course.objects.annotate(topic_count=Count("topics"))

# serializers.py
class CourseSerializer(serializers.ModelSerializer):
    topic_count = serializers.IntegerField(read_only=True)
```

The usual moves, all of them from the serializer into the view's `get_queryset()`:

| In a serializer method | Move it to |
| ---------------------- | ---------- |
| `.count()`, when you don't need the rows | `.annotate(Count(...))` |
| `.exists()` | `.annotate(Exists(...))` |
| `instance.related.all()` | `prefetch_related("related")`, then a nested serializer or a plain loop |
| `instance.related.filter(...)` | `Prefetch("related", queryset=..., to_attr=...)` |
| `instance.related.order_by(...).first()` | an ordered `Prefetch`, then index `[0]` in Python |
| `Model.objects.get(...)` | `select_related()`, or accept the id and let the client resolve it |

Two things that are easy to miss:

- `.count()` and `.exists()` are free on a prefetched relation - the related
  manager hands back the warm cache, and `QuerySet.count()` just measures it.
  `.annotate(Count(...))` still wins when the count is all you need, because it
  never fetches the rows. But adding `.filter()`, `.order_by()`, or `.exclude()`
  discards the cache and puts you back to one query per object.
- `__init__` and `to_representation()` count too. The rule is about the whole
  serializer, not just `SerializerMethodField`.

Logic that genuinely can't move into the queryset - anything a non-API caller also
needs - belongs behind a
[same-named `cached_property`](prefetching.md#make-one-property-work-with-and-without-a-prefetch)
on the model, so the serializer still just reads an attribute.

drf-lint enforces this rule statically in CI and pre-commit; see
[testing-and-lint.md](testing-and-lint.md#lint-serializers-with-drf-lint).

## Require prefetches in serializers

Subclass `mitol.common.serializers.BaseSerializer` and define
`required_prefetches`. If you don't define it, a
`RequiredPrefetchesNotDefinedError` is raised on serializer init:

```python
from mitol.common.serializers import BaseSerializer


class EnrollmentSerializer(BaseSerializer):
    required_prefetches: list[str] = [
        "program_titles"
    ]
```

This ensures the serializer can't be used without that prefetch having been done -
otherwise it raises a `RequiredPrefetchMissingError` naming the prefetch that
wasn't requested. A "prefetch" in this situation is anything that should be
`prefetch()`, `prefetch_related()`, or `select_related()`.

> **The escape hatch is not for API code.** Tests and async code such as celery
> tasks can opt out by passing `{"skip_prefetch_checks": THIS_IS_NOT_AN_API}`. As
> the name (and you) attests to, this should not be used anywhere near an API -
> including when other serializers call it, since DRF propagates the context into
> child serializers as well.
