#!/usr/bin/env bash
# TEMPLATE - adapt to the app under test.
#
# A/B an endpoint on the current git ref vs a baseline ref, against ONE seeded
# throwaway database. Untracked; add .bench/ to .git/info/exclude.
#
# Usage:  .bench/run.sh [baseline-ref]
#         BENCH_BLOB_BYTES=8192 .bench/run.sh    # re-run at another shape
set -euo pipefail

# App-specific settings overrides the seed needs - most commonly turning off
# remote object storage, or the factories will try to upload images:
#   BENCH_EXTRA_ENV='-e MYAPP_USE_S3=False'
BASE_REF=${1:-main}
SERVICE=${BENCH_SERVICE:-web}
BENCH_DB=${BENCH_DB:-bench_db}
DB_URL="postgres://postgres:postgres@db:5432/${BENCH_DB}"
BRANCH=$(git branch --show-current)
OUT_DIR=.bench/out
mkdir -p "$OUT_DIR"

if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty; commit or stash before benchmarking" >&2
  exit 1
fi

dc_run() {
  docker compose run --rm --no-deps \
    -e DATABASE_URL="$DB_URL" \
    -e DEBUG=False \
    -e BENCH_IDS_PATH=/src/.bench/ids.json \
    ${BENCH_EXTRA_ENV:-} \
    ${BENCH_BLOB_BYTES:+-e BENCH_BLOB_BYTES="$BENCH_BLOB_BYTES"} \
    "$@" "$SERVICE"
}

if [ "${BENCH_SKIP_SEED:-0}" != "1" ]; then
  echo "==> recreating ${BENCH_DB}"
  docker compose exec -T db psql -U postgres -q \
    -c "DROP DATABASE IF EXISTS ${BENCH_DB}" \
    -c "CREATE DATABASE ${BENCH_DB}" >/dev/null

  echo "==> migrating"
  dc_run python manage.py migrate --no-input >/dev/null

  echo "==> seeding"
  dc_run python manage.py shell <.bench/seed.py | tee "$OUT_DIR/seed.txt" | grep SEED_SHAPE
fi

run_arm() {
  local label=$1
  echo "==> benchmarking ${label}"
  dc_run -e BENCH_LABEL="$label" python manage.py shell <.bench/bench.py |
    grep BENCH_RESULT | sed 's/^BENCH_RESULT //' >"$OUT_DIR/${label}.json"
  dc_run -e BENCH_LABEL="$label" python manage.py shell <.bench/trace.py |
    grep TRACE_SUMMARY >/dev/null
  cat "$OUT_DIR/${label}.json"
}

# Seed once, switch refs around it: that is what makes this an A/B rather than
# two unrelated measurements.
run_arm "branch"

echo "==> switching to ${BASE_REF}"
git switch --quiet "$BASE_REF"
trap 'git switch --quiet "$BRANCH"' EXIT
run_arm "base"
git switch --quiet "$BRANCH"
trap - EXIT
echo "==> back on ${BRANCH}"

echo
echo "=== wall clock ==="
jq -rs '
  .[0] as $base | .[1] as $branch |
  ["total_ms_min","total_ms_median","sql_ms_capture_pass","python_ms_est",
   "queries","response_bytes","count","results"] as $rows |
  (["metric","base","branch","delta"] | @tsv),
  ($rows[] | [., ($base[.]|tostring), ($branch[.]|tostring),
              (($branch[.] - $base[.]) * 100 | round / 100 | tostring)] | @tsv)
' "$OUT_DIR/base.json" "$OUT_DIR/branch.json" | column -t

echo
echo "!! response_bytes / count / results must match between arms."
echo "!! If they differ, the comparison is void - find out why before quoting a delta."

echo
echo "=== per-query attribution (median of traced repeats) ==="
for arm in base branch; do
  n=$(jq '.repeats' "$OUT_DIR/trace-$arm.json")
  jq --arg n "$n" -f .bench/agg.jq "$OUT_DIR/trace-$arm.json" >"$OUT_DIR/agg-$arm.json"
done
jq -rs '
  (.[0]|INDEX(.k)) as $b | (.[1]|INDEX(.k)) as $c |
  (["query","base_sql","base_gap","base_tot","br_sql","br_gap","br_tot","delta"]|@tsv),
  ((.[0]+.[1]|map(.k)|unique)[] as $k |
    ($b[$k] // {sql:0,gap:0,tot:0}) as $x | ($c[$k] // {sql:0,gap:0,tot:0}) as $y |
    [$k,($x.sql|tostring),($x.gap|tostring),($x.tot|tostring),
        ($y.sql|tostring),($y.gap|tostring),($y.tot|tostring),
        ((($y.tot-$x.tot)*10|round/10)|tostring)]|@tsv)
' "$OUT_DIR/agg-base.json" "$OUT_DIR/agg-branch.json" | column -t -s$'\t'
