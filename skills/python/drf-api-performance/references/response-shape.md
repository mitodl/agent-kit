# Response shape reference

Detail behind the nesting rule in [SKILL.md](../SKILL.md).

## Keep nesting to two levels

As a rule of thumb, an API shouldn't return an object structure deeper than two
levels. If you find yourself needing more depth, that's a signal consumers should
be making a subsequent call to another API instead.

Avoid this - `GET /api/enrollments/`:

```json
[{
    "id": 1,
    "course": {
        "id": 234,
        "topics": [{
            "name": "Chemistry"
        }]
    }
}, {
    "id": 2,
    "course": {
        "id": 62,
        "topics": [{
            "name": "Physics"
        }]
    }
}]
```

Two things go wrong here:

- More deeply nested responses are increasingly difficult to write optimized
  queries for.
- Data is duplicated in-memory on the clients, resulting in a larger runtime
  footprint and worse performance.

Do this instead - normalize the data and split it across two calls:

```json
// GET /api/enrollments/
[{
    "id": 1,
    "course_id": 234
}, ...]
```

```json
// GET /api/courses/?id=234,62
[{
    "id": 62,
    "topics": [{
        "name": "Physics"
    }]
}, {
    "id": 234,
    "topics": [{
        "name": "Chemistry"
    }]
}]
```

## Trade-off: one extra round trip

The client makes an extra network request, and gets two things back:

- A much faster initial request, because it's loading less data.
- A cacheable second request - the results from `/api/courses/?id=234,62` may in
  fact already be loaded and not need to be requested at all.
