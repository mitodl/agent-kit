#!/usr/bin/env bash
# TEMPLATE - adapt to the app under test.
#
# A/B an endpoint on the current git ref vs a baseline ref, against ONE seeded
# throwaway database. Untracked; add .bench/ to .git/info/exclude.
#
# The execution environment lives behind .bench/backend.sh - copy one of
# references/backends/{compose,k8s-tilt}.sh there. See environments.md.
#
# Usage:  .bench/run.sh [baseline-ref]
#         BENCH_BLOB_BYTES=8192 .bench/run.sh    # re-run at another shape
set -euo pipefail

# App-specific settings overrides the seed needs - most commonly turning off
# remote object storage, or the factories will try to upload images. A
# space-separated VAR=VAL list, identical across backends:
#   BENCH_EXTRA_ENV='MYAPP_USE_S3=False'
BASE_REF=${1:-main}
BENCH_DB=${BENCH_DB:-bench_db}
BRANCH=$(git branch --show-current)
OUT_DIR=.bench/out
mkdir -p "$OUT_DIR"

# The scratch database is dropped and recreated below. Refuse any name that is
# not obviously a scratch one, which also rules out every app database in a
# shared cluster (mitlearn, mitxonline, ...).
case "$BENCH_DB" in
  bench*) ;;
  *) echo "refusing: BENCH_DB='${BENCH_DB}' must start with 'bench'" >&2; exit 1 ;;
esac

if [ ! -f .bench/backend.sh ]; then
  echo "missing .bench/backend.sh - copy a backend from the skill's references/backends/" >&2
  exit 1
fi
# shellcheck source=/dev/null
source .bench/backend.sh

if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty; commit or stash before benchmarking" >&2
  exit 1
fi

# Backends may define bench_wait_settled to block until the app environment has
# finished reacting to a source change. Bind-mount backends need nothing.
if ! declare -F bench_wait_settled >/dev/null; then
  bench_wait_settled() { :; }
fi

app_exec() {
  bench_exec \
    DATABASE_URL="$BENCH_DB_URL" \
    DEBUG=False \
    ${BENCH_BLOB_BYTES:+BENCH_BLOB_BYTES="$BENCH_BLOB_BYTES"} \
    "$@"
}

# Prove the app environment is actually running the ref we just switched to,
# rather than trusting that a `git switch` took effect. Instant under a bind
# mount; blocks through Tilt's push-sync (and any `uv sync` it triggers) under
# Kubernetes. Compares files the two refs genuinely differ on, so it cannot
# pass by accident.
bench_sync_ref() {
  local want=$1 deadline=$((SECONDS + ${BENCH_SYNC_TIMEOUT:-300})) f
  local -a probes=()
  while IFS= read -r f; do
    if [ -f "$f" ]; then probes+=("$f"); fi
  done < <(git diff --name-only "$BASE_REF" "$BRANCH" -- '*.py' | head -3)

  if [ ${#probes[@]} -eq 0 ]; then
    echo "!! no .py file differs between ${BASE_REF} and ${BRANCH}; cannot verify the arm ran ${want}" >&2
    return 0
  fi
  for f in "${probes[@]}"; do
    until bench_exec -- cat "$f" 2>/dev/null | cmp -s - "$f"; do
      if [ "$SECONDS" -ge "$deadline" ]; then
        echo "timed out waiting for ${f} to reach the app environment at ${want}" >&2
        exit 1
      fi
      sleep 2
    done
  done
  bench_wait_settled
}

if [ "${BENCH_SKIP_SEED:-0}" != "1" ]; then
  echo "==> target: ${BENCH_TARGET_DESC}"
  echo "==> recreating ${BENCH_DB}"
  bench_psql "DROP DATABASE IF EXISTS ${BENCH_DB}" "CREATE DATABASE ${BENCH_DB}" >/dev/null

  echo "==> migrating"
  app_exec -- python manage.py migrate --no-input >/dev/null

  echo "==> seeding"
  # SEED_SHAPE is both the human record of the shape and the machine handoff to
  # the bench/trace steps - it is json.dumps(ids). Results travel on stdout, not
  # through a shared filesystem, because not every backend has one.
  app_exec -- python manage.py shell <.bench/seed.py |
    tee "$OUT_DIR/seed.txt" |
    sed -n 's/^SEED_SHAPE //p' >"$OUT_DIR/ids.json"
  cat "$OUT_DIR/ids.json"
fi

if [ ! -s "$OUT_DIR/ids.json" ]; then
  echo "no seeded ids at $OUT_DIR/ids.json; re-run without BENCH_SKIP_SEED=1" >&2
  exit 1
fi
IDS_JSON=$(cat "$OUT_DIR/ids.json")

run_arm() {
  local label=$1
  echo "==> benchmarking ${label} ($(git rev-parse --short HEAD))"
  git rev-parse --short HEAD >"$OUT_DIR/${label}.ref"
  app_exec BENCH_IDS_JSON="$IDS_JSON" BENCH_LABEL="$label" \
    -- python manage.py shell <.bench/bench.py |
    sed -n 's/^BENCH_RESULT //p' >"$OUT_DIR/${label}.json"
  app_exec BENCH_IDS_JSON="$IDS_JSON" BENCH_LABEL="$label" \
    -- python manage.py shell <.bench/trace.py |
    sed -n 's/^TRACE_RESULT //p' >"$OUT_DIR/trace-${label}.json"
  cat "$OUT_DIR/${label}.json"
}

# Seed once, switch refs around it: that is what makes this an A/B rather than
# two unrelated measurements.
bench_sync_ref "$BRANCH"
run_arm "branch"

echo "==> switching to ${BASE_REF}"
git switch --quiet "$BASE_REF"
trap 'git switch --quiet "$BRANCH"' EXIT
bench_sync_ref "$BASE_REF"
run_arm "base"
git switch --quiet "$BRANCH"
trap - EXIT
bench_sync_ref "$BRANCH"
echo "==> back on ${BRANCH}"

echo
echo "measured against: ${BENCH_TARGET_DESC}"
echo "arms: base=$(cat "$OUT_DIR/base.ref") branch=$(cat "$OUT_DIR/branch.ref")"

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
