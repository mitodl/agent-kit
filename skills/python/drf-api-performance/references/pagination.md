# Pagination reference

Detail behind the pagination rules in [SKILL.md](../SKILL.md).

## Paginate every list endpoint

Every list endpoint must be paginated. An unpaginated endpoint is a latency and
memory cliff that grows with your data - it passes review, passes tests, passes
RC, and then falls over in production when the table it reads is an order of
magnitude larger. "This table is small" is a statement about today, not about the
endpoint.

### Set one default, override per view

Define pagination once in a shared module rather than per app. Before
[mit-learn#3106](https://github.com/mitodl/mit-learn/pull/3106), an identical
`DefaultPagination` had been copy-pasted into several apps' `views.py`, so there
was no single place to fix any of this:

```python
# main/pagination.py
from rest_framework.pagination import LimitOffsetPagination


class DefaultPagination(LimitOffsetPagination):
    """Default pagination class for rest APIs"""

    count_fields = ("pk",)

    default_limit = 10
    max_limit = 100

    def get_count(self, queryset):
        """Get the count of objects in the queryset"""
        # we additionally filter this down to a subset of fields
        return queryset.only(*self.count_fields).count()


class LargePagination(DefaultPagination):
    """Large pagination for small resources, e.g., topics."""

    default_limit = 1000
    max_limit = 1000
```

`max_limit` is not optional. Without it, `?limit=100000` defeats the pagination
you just configured.

Register that class as the framework default in `settings.py`, so a new viewset
arrives paginated without opting in:

```python
REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "main.pagination.DefaultPagination",
}
```

Two things to know about the setting:

- It's a dotted path, not an import. DRF resolves it on first access, so
  `main/pagination.py` can import from the rest of the project without creating a
  circular import while settings load.
- Naming a default class doesn't by itself guarantee pagination.
  `PageNumberPagination` takes its page size from the `PAGE_SIZE` setting and
  returns everything when that is unset. `DefaultPagination` above sidesteps this
  by declaring `default_limit` on the class, which is where you want it anyway -
  next to `max_limit`, not in settings.

With the default registered, delete the per-viewset
`pagination_class = DefaultPagination` lines. They are redundant, and they are the
copy-paste that drifts. From then on a `pagination_class` on a view means "this
one is deliberately different", in one of three ways:

```python
# 1. A different page size - reuse a class from the shared module
class AttestationViewSet(viewsets.ReadOnlyModelViewSet):
    pagination_class = LargePagination


# 2. A different count query - subclass next to the view that needs it
class SummaryPagination(LargePagination):
    """LargePagination that keeps annotations out of the count query."""

    def get_count(self, queryset):
        """Count distinct pks; .values() drops the annotation, .only() would not"""
        return queryset.values(*self.count_fields).distinct().count()


# 3. No pagination at all - stated, not implied
class UserSearchSubscriptionViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    pagination_class = None  # unpaginated by design; preserves the existing interface
```

All three are visible in review, which is the point: an exemption should be a line
somebody approved, not the absence of a setting. Subclass `DefaultPagination`
rather than `LimitOffsetPagination` so the cheap `get_count()` and the `max_limit`
cap come along - `LargePagination` is exactly that, two attributes over an
inherited body.

### Order deterministically or pages will lie

Pagination on a non-deterministic ordering silently duplicates and skips records
across pages, because the database is free to return equal-ranked rows in any
order. Always order on something unique, or append a unique tiebreaker:

```python
queryset = Enrollment.objects.order_by("-created_on", "id")
```

## Pick the right pagination class

| Class | Use it for | Watch out for |
| ----- | ---------- | ------------- |
| `LimitOffsetPagination` | The default for most list APIs | Deep offsets get slower; computes `count` |
| `PageNumberPagination` | APIs whose clients think in page numbers | Same as above |
| `CursorPagination` | Large or append-heavy tables, feeds, exports | Requires a stable indexed ordering; no random access to page N |

`LIMIT`/`OFFSET` does not skip rows for free - Postgres still walks and discards
everything before the offset, so page 500 costs far more than page 1.
`CursorPagination` instead filters on an indexed ordering column, so every page
costs the same, and it omits `count` entirely.

## `count` is a second query over your whole result set

DRF's paginated response includes a `count`, computed as a separate
`SELECT COUNT(*)` wrapping your queryset. That is cheap for a plain filter and
expensive once the query carries aggregations or wide joins - and the cost scales
with production data, not with your fixtures.

This is not hypothetical. It is the proximate cause of the
[2026-03-24 MIT Learn outage](https://engineering.ol.mit.edu/runbooks_post_mortems/20260324_mitlearn_outage/),
where moving an aggregation into the main query was fine locally and on RC, then
exhausted the production database's temp space and storage under real cardinality.

DRF's default `get_count()` is just `queryset.count()`. When the queryset is
`DISTINCT` and carries annotations, that count wraps a subquery selecting **every
column the page would have selected** - including the joins and aggregates that
exist only to produce them. Counting rows needs none of that. For
`/api/v1/featured/` the count query looked like this:

```sql
SELECT COUNT(*)
FROM (
  SELECT DISTINCT "learningresource"."id" AS "col1",
    "learningresource"."created_on" AS "col2",
    -- ... 37 more columns ...
    COUNT("learningresourceviewevent"."id") AS "_views_count",
    "learningresourcerelationship"."position" AS "position"
  FROM "learningresource"
  LEFT OUTER JOIN "learningresourceviewevent" ON (...)
  INNER JOIN "learningresourcerelationship" ON (...)
  WHERE (...)
)
```

Narrowing the count to the primary key - the `get_count()` override in
`DefaultPagination` - reduces it to this, dropping the view-event join and its
aggregate entirely:

```sql
SELECT COUNT(*)
FROM (
  SELECT DISTINCT "learningresource"."id" AS "col1",
    "learningresourcerelationship"."position" AS "position"
  FROM "learningresource"
  INNER JOIN "learningresourcerelationship" ON (...)
  WHERE (...)
) subquery;
```

On that endpoint the count went from ~500ms to ~0.3ms. Don't expect ~1000x
everywhere - the win is that the count stops going to disk for data it never
needed and can often be served from indexes alone.

## `.only()` vs `.values()` when the queryset has annotations

`.only()` narrows the columns Django loads, but it does **not** reliably strip
annotations out of the count subquery, and an annotation left in there is
evaluated once per row counted. `.values()` does strip them.

If the queryset a view builds carries annotations, subclass and swap `.only()`
for `.values()` - that is why mit-learn's real `SummaryPagination` exists:

```python
class SummaryPagination(LargePagination):
    """LargePagination that keeps annotations out of the count query."""

    def get_count(self, queryset):
        """Count distinct pks; .values() drops the annotation, .only() would not"""
        return queryset.values(*self.count_fields).distinct().count()
```

## Widen `count_fields` when DISTINCT is doing real work

`count_fields` is a class attribute precisely so it can be overridden. Narrowing
to `pk` preserves the count when the `DISTINCT` already includes a unique column,
because the row count can't change.

If a queryset is `DISTINCT` over **non-unique** columns specifically to collapse
duplicate rows, then dropping columns changes what "distinct" means and therefore
changes the count. Subclass and widen `count_fields` to include the columns the
distinctness depends on.

If you need an aggregate in the response, prefer computing it outside the
paginated queryset, or use `CursorPagination`, which doesn't count at all.
