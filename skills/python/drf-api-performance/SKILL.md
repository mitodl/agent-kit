---
name: drf-api-performance
description: >
  Write and review fast Django REST Framework APIs. Use this skill when adding or
  changing a DRF viewset, serializer, pagination class, or queryset - covers
  response nesting limits, paginating list endpoints and keeping the count query
  cheap, select_related vs prefetch_related vs django-prefetch, keeping ORM
  queries out of serializers, required_prefetches, and catching N+1s with
  django-zeal, django_assert_num_queries, and drf-lint.
license: BSD-3-Clause
metadata:
  category: python
---

# Performant DRF APIs

A fast API is two problems: the **shape** of the response, and the **cost of the
queries** that fill it. These rules apply to every DRF viewset, serializer, and
queryset.

## The rules

- Keep response nesting to **two levels or less**; split deeper data into a second endpoint.
- **Paginate every list endpoint.** Set the default once in a shared module, cap
  client-supplied page size with `max_limit`, and order deterministically.
- **Narrow the pagination count query to the primary key.** It is a second query
  over your whole result set, and it scales with production data, not fixtures.
- Pick the **narrowest prefetch tool** that works: `select_related()` for foreign
  keys, `prefetch_related()` for to-many relationships, `prefetch()` for data the
  model has no direct relationship to.
- Use `select_related()`, not `prefetch_related()`, when the queryset **already
  filters or orders on that table** - the join is happening either way.
- Three or four to-one joins are fine; **eight is the limit** where you split the
  query instead of widening it.
- **Never query inside a serializer** - the body runs once per object, so a query
  there is multiplied by the page size. The view's queryset assembles the data.
- Declare `required_prefetches` on every serializer so a missing prefetch fails
  loudly instead of silently issuing one query per row.
- Back a prefetch with a **same-named `cached_property`** so non-API callers get
  the same answer without a second implementation.
- Test list APIs with **5-10 records at each level**, or the N+1 checks won't fire.
- Pin a **constant query count across varying data** with
  `django_assert_num_queries`, and never add `skip_nplusone_check` to a new test.

## References

| Read this | For |
| --------- | --- |
| [response-shape.md](references/response-shape.md) | The two-level nesting rule, worked normalization example, the extra-round-trip trade-off |
| [pagination.md](references/pagination.md) | `DefaultPagination` in a shared module, `DEFAULT_PAGINATION_CLASS`, the three legitimate per-view overrides, class comparison, why the count query gets expensive, `.only()` vs `.values()`, widening `count_fields` |
| [prefetching.md](references/prefetching.md) | Tool comparison, the already-joined exception, writing a `prefetch()` prefetcher and its footguns, composite keys, the `cached_property` shadowing pattern and the `hasattr` antipattern |
| [joins-and-query-plans.md](references/joins-and-query-plans.md) | Width vs multiplication, when table size enters the plan, Postgres planner thresholds, reading `EXPLAIN (ANALYZE, BUFFERS)`, getting the SQL out of Django |
| [serializers.md](references/serializers.md) | The `SerializerMethodField` N+1, the full "move it to the queryset" table, `BaseSerializer` and `required_prefetches` |
| [testing-and-lint.md](references/testing-and-lint.md) | django-zeal setup and scoped exemptions, `django_assert_num_queries` vs `django_assert_max_num_queries`, drf-lint's ORM001/ORM002 and its baseline |

## Resources

- [Write Performant APIs](https://engineering.ol.mit.edu/handbook/how-to/write-performant-apis/) - the handbook page this skill is drawn from
- [2026-03-24 MIT Learn outage post-mortem](https://engineering.ol.mit.edu/runbooks_post_mortems/20260324_mitlearn_outage/) - what an expensive count query costs in production
- [django-prefetch](https://pypi.org/project/django-prefetch/), [django-zeal](https://pypi.org/project/django-zeal/), [mitol-drf-lint](https://github.com/mitodl/ol-django/tree/main/src/drf_lint)
- [DRF: Pagination](https://www.django-rest-framework.org/api-guide/pagination/), [Django: `prefetch_related()`](https://docs.djangoproject.com/en/stable/ref/models/querysets/#prefetch-related), [Django: `Prefetch` objects](https://docs.djangoproject.com/en/stable/ref/models/querysets/#prefetch-objects), [Django: `cached_property`](https://docs.djangoproject.com/en/stable/ref/utils/#django.utils.functional.cached_property)
