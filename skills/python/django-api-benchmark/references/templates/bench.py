"""
TEMPLATE - adapt to the app under test.

Wall-clock A/B of one endpoint against the seeded bench database. Read-only.
Run via:  manage.py shell < .bench/bench.py

Prints one `BENCH_RESULT {json}` line. Run on each ref against the SAME seeded
database; the two runs are comparable because the rows are identical.
"""

import json
import os
import statistics
import time

from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

# from myapp.models import User

WARMUP = int(os.environ.get("BENCH_WARMUP", "3"))
ITERATIONS = int(os.environ.get("BENCH_ITERATIONS", "15"))
# Default deliberately outside the source tree: under an auto-reloading
# dev server that watches it, writing there re-imports Django mid-run.
DEFAULT_IDS_PATH = "/tmp/bench-ids.json"  # noqa: S108
LABEL = os.environ.get("BENCH_LABEL", "unknown")

# --- preconditions: refuse to run rather than produce a wrong number ---------
# An N+1 profiler hooks every ORM fetch; that cost scales with objects
# hydrated, so it inflates whichever arm loads more rows.
profilers = [m for m in settings.MIDDLEWARE if "zeal" in m or "nplusone" in m]
if profilers:
    raise SystemExit(f"profiler middleware active ({profilers}); would skew the A/B")
if settings.DEBUG:
    raise SystemExit("DEBUG is on; Django would record every query and skew timings")


def _load_ids():
    """
    Results travel between harness steps over stdout/env, never through a
    shared filesystem: not every execution backend has one. BENCH_IDS_JSON is
    what run.sh passes; BENCH_IDS_PATH is the fallback for running a step by
    hand.
    """
    raw = os.environ.get("BENCH_IDS_JSON")
    if raw:
        return json.loads(raw)
    with open(os.environ.get("BENCH_IDS_PATH", DEFAULT_IDS_PATH)) as fh:  # noqa: PTH123
        return json.load(fh)


ids = _load_ids()

client = APIClient()
# client.force_authenticate(user=User.objects.get(pk=ids["user_id"]))

url = reverse("<namespace>:<url-name>")
params = {"<filter>": ids["filter_id"], "page_size": 100}


def call():
    resp = client.get(url, params)
    if resp.status_code != 200:  # noqa: PLR2004
        msg = f"expected 200, got {resp.status_code}: {resp.content[:400]!r}"
        raise SystemExit(msg)
    return resp


# Warm-up absorbs content-type caches, CMS site-root caches, first-call imports.
for _ in range(WARMUP):
    call()

timings = []
for _ in range(ITERATIONS):
    start = time.perf_counter()
    response = call()
    timings.append((time.perf_counter() - start) * 1000)

body = response.json()

# Separate pass. CaptureQueriesContext forces a debug cursor, which times every
# execute and retains every SQL string - real overhead that grows with
# statement size, so it must not contaminate the wall-clock numbers above.
with CaptureQueriesContext(connection) as ctx:
    captured = call()
sql_ms = sum(float(q["time"]) for q in ctx.captured_queries) * 1000

result = {
    "label": LABEL,
    "iterations": ITERATIONS,
    "total_ms_min": round(min(timings), 1),
    "total_ms_median": round(statistics.median(timings), 1),
    "total_ms_max": round(max(timings), 1),
    "queries": len(ctx.captured_queries),
    "sql_ms_capture_pass": round(sql_ms, 1),
    # Row fetch + model instantiation: what over-fetching actually costs.
    "python_ms_est": round(min(timings) - sql_ms, 1),
    # Equivalence fields. If these differ between arms the comparison is void.
    "response_bytes": len(captured.content),
    "count": body.get("count"),
    "results": len(body.get("results", [])),
    # "nested_serialized": sum(len(r["children"]) for r in body["results"]),
    # Shape, echoed so the number is never quoted without it.
    "blob_bytes": ids.get("blob_bytes"),
    "children_seeded": ids.get("children"),
}
print("BENCH_RESULT " + json.dumps(result))  # noqa: T201
