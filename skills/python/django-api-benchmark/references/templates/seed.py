"""
TEMPLATE - adapt to the app under test.

Seed a production-shaped dataset for an endpoint A/B benchmark.
Run via:  manage.py shell < .bench/seed.py

Writes BENCH_IDS_PATH so bench.py / trace.py know what to request.

Rules this template encodes:
  * every shape dimension is an env var, so re-running at another shape needs
    no edit and the report can state the exact shape
  * the resulting shape is printed as one machine-readable line
  * a floor derived from the production trace is checked, and warned about

Replace the CAPITALISED placeholders and the build() body. Keep the structure:
the header comment is where you record which parts of the shape are OBSERVED
(from responses / traces) and which are GUESSES, because the report has to
distinguish them.
"""

import json
import os
import sys

from django.db import transaction

# from myapp.factories import ParentFactory, ChildFactory
# from myapp.models import Child

# --- shape knobs: one env var each -------------------------------------------
# OBSERVED from sample responses:
PARENTS = int(os.environ.get("BENCH_PARENTS", "25"))
CHILDREN_PER_PARENT = int(os.environ.get("BENCH_CHILDREN_PER_PARENT", "16"))
NESTED_PER_PARENT = int(os.environ.get("BENCH_NESTED_PER_PARENT", "13"))
# OBSERVED from the trace (row-count floor):
NOISE_PARENTS = int(os.environ.get("BENCH_NOISE_PARENTS", "200"))
# GUESSES - nothing in a response or trace can show these. Identical in both
# arms, so they move the baseline, not the delta. Say so in the write-up.
BLOB_BYTES = int(os.environ.get("BENCH_BLOB_BYTES", "2048"))

# Floor from the trace's IN-list placeholder count. Truncated exports make this
# a lower bound, not a count.
ROW_FLOOR = int(os.environ.get("BENCH_ROW_FLOOR", "388"))

OUT = os.environ.get("BENCH_IDS_PATH", "/src/.bench/ids.json")


def html_blob(nbytes):
    """A chunk of plausible rich text of roughly nbytes."""
    para = (
        "<p>Placeholder body copy standing in for the real rich text this "
        "column holds in production, so the row width is representative.</p>"
    )
    return (para * (nbytes // len(para) + 1))[:nbytes]


@transaction.atomic
def build():
    """
    Build the dataset and return the ids + shape the bench scripts need.

    STRUCTURE IS WHAT MATTERS. Before tuning payload sizes, get the fan-out
    right: how children distribute across tenants, how many rows a filter
    matches versus how many a prefetch loads. Putting everything under one
    tenant is the classic error - it makes the filter match everything and
    turns an unchanged query pathological. See calibration.md.
    """
    blob = html_blob(BLOB_BYTES)  # noqa: F841

    # 1. Tenants / orgs - MANY of them, so filters stay selective.
    # 2. The filtered-on object, attached to every parent that must match.
    # 3. Parents, each with NESTED_PER_PARENT nested rows.
    # 4. Children spread across tenants: only some match the filter, all are
    #    loaded by the prefetch.
    # 5. Noise parents, so the main query is not scanning a toy table.
    # 6. A user with whatever membership the endpoint's permission check needs
    #    - filters often return an empty queryset without it.

    raise NotImplementedError("fill in for the app under test")

    return {  # noqa: W0101
        "filter_id": None,
        "user_id": None,
        "parents": PARENTS,
        "children": 0,
        "blob_bytes": BLOB_BYTES,
    }


ids = build()
# Record derived counts too - join multiplicity is invisible in a response, so
# printing it is the only way the report can state it.
# ids["m2m_pairs"] = Child.tenants.through.objects.count()

with open(OUT, "w") as fh:  # noqa: PTH123
    json.dump(ids, fh, indent=2)

print("SEED_SHAPE " + json.dumps(ids))  # noqa: T201
if ids.get("children", 0) < ROW_FLOOR:
    print(  # noqa: T201
        f"WARNING: {ids.get('children')} child rows is below the trace floor "
        f"of {ROW_FLOOR}; the seed understates production fan-out",
        file=sys.stderr,
    )
