"""
TEMPLATE - adapt to the app under test.

Capture an in-process OTel trace of one endpoint against the seeded bench
database, for per-query attribution. Run via:  manage.py shell < .bench/trace.py

Emits .bench/out/trace-$BENCH_LABEL.json: every span with start/end in
nanoseconds plus the gap to the next query, so the same analysis used on the
production trace applies.

NOTE: instrumentation is not free, and app telemetry is normally disabled
without an OTLP endpoint - which is what makes bench.py's timings clean.
Absolute numbers here are NOT the benchmark numbers. This is for attribution
(where the time goes); bench.py is for the headline.
"""

import json
import os

from django.conf import settings

# Instrument BEFORE anything builds a request handler or opens a connection:
# the Django instrumentor inserts middleware, and a handler caches its chain.
from opentelemetry import trace as otel_trace
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

LABEL = os.environ.get("BENCH_LABEL", "unknown")
IDS_PATH = os.environ.get("BENCH_IDS_PATH", "/src/.bench/ids.json")
REPEATS = int(os.environ.get("BENCH_TRACE_REPEATS", "7"))
OUT = f"/src/.bench/out/trace-{LABEL}.json"

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
otel_trace.set_tracer_provider(provider)

DjangoInstrumentor().instrument()
# psycopg2 apps: opentelemetry.instrumentation.psycopg2 instead.
PsycopgInstrumentor().instrument(skip_dep_check=True)

from django.db import connection  # noqa: E402
from django.urls import reverse  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

# from myapp.models import User  # noqa: ERA001

# Force a reconnect so the instrumented connect() is the one used.
connection.close()

with open(IDS_PATH) as fh:  # noqa: PTH123
    ids = json.load(fh)

client = APIClient()
# client.force_authenticate(user=User.objects.get(pk=ids["user_id"]))
url = reverse("<namespace>:<url-name>")
params = {"<filter>": ids["filter_id"], "page_size": 100}


def call():
    resp = client.get(url, params)
    if resp.status_code != 200:  # noqa: PLR2004
        msg = f"expected 200, got {resp.status_code}"
        raise SystemExit(msg)
    return resp


for _ in range(3):
    call()

runs = []
for _ in range(REPEATS):
    exporter.clear()
    response = call()
    spans = sorted(
        (
            {
                "name": s.name,
                "start": s.start_time,
                "end": s.end_time,
                "dur_ms": round((s.end_time - s.start_time) / 1e6, 2),
                "sql": (dict(s.attributes or {}).get("db.statement") or "")[:4000],
            }
            for s in exporter.get_finished_spans()
        ),
        key=lambda r: r["start"],
    )
    root_end = max(s["end"] for s in spans)
    db = [s for s in spans if s["sql"]]
    # Gap after each query = wall time until the next query, or until the end
    # of the request for the last one. The span wraps execute() only, so row
    # fetch, model instantiation and serialization all live in the gaps.
    for i, s in enumerate(db):
        nxt = db[i + 1]["start"] if i + 1 < len(db) else root_end
        s["gap_ms"] = round((nxt - s["end"]) / 1e6, 2)
    runs.append({"wall_ms": round((root_end - db[0]["start"]) / 1e6, 1), "db": db})

os.makedirs(os.path.dirname(OUT), exist_ok=True)  # noqa: PTH103, PTH120
with open(OUT, "w") as fh:  # noqa: PTH123
    json.dump(
        {
            "label": LABEL,
            "repeats": REPEATS,
            "profiler_active": any("zeal" in m for m in settings.MIDDLEWARE),
            "response_bytes": len(response.content),
            "runs": runs,
        },
        fh,
        indent=1,
    )

print(  # noqa: T201
    "TRACE_SUMMARY "
    + json.dumps(
        {
            "label": LABEL,
            "repeats": REPEATS,
            "db_spans": len(runs[0]["db"]),
            "wall_ms_each": [r["wall_ms"] for r in runs],
            "out": OUT,
        }
    )
)
