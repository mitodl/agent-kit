# TEMPLATE - execution backend: this repo's own `docker compose` stack.
# Copy to .bench/backend.sh; run.sh sources that fixed path.
#
# Supplies the contract described in references/environments.md:
#   bench_exec VAR=VAL ... -- <cmd>   run <cmd> in the app's Django environment
#   bench_psql <sql> [<sql> ...]      admin SQL, connected to a DB that is not $BENCH_DB
#   BENCH_DB_URL                      the scratch DSN as the app process sees it
#   BENCH_TARGET_DESC                 what is about to be written to, for the report
#
# Assumes `docker compose up` has already been run: every step here uses
# --no-deps and nothing starts or stops services for you.

BENCH_SERVICE=${BENCH_SERVICE:-web}
BENCH_DB_SERVICE=${BENCH_DB_SERVICE:-db}
BENCH_PG_USER=${BENCH_PG_USER:-postgres}
BENCH_PG_PASSWORD=${BENCH_PG_PASSWORD:-postgres}
BENCH_PG_ADMIN_DB=${BENCH_PG_ADMIN_DB:-postgres}

# As resolved inside the app container, where `db` is the compose service name.
BENCH_DB_URL="postgres://${BENCH_PG_USER}:${BENCH_PG_PASSWORD}@${BENCH_DB_SERVICE}:5432/${BENCH_DB}"
BENCH_TARGET_DESC="docker compose: service '${BENCH_SERVICE}', database '${BENCH_DB}' on service '${BENCH_DB_SERVICE}'"

# Each step gets a fresh throwaway container with nothing else running in it -
# the cleanest measurement environment of the two backends.
bench_exec() {
  local envs=() kv
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
    envs+=(-e "$1")
    shift
  done
  shift  # the --
  for kv in ${BENCH_EXTRA_ENV:-}; do envs+=(-e "$kv"); done
  # -T: no TTY, so `manage.py shell < script.py` on stdin works.
  docker compose run --rm --no-deps -T "${envs[@]}" "$BENCH_SERVICE" "$@"
}

bench_psql() {
  local args=() sql
  for sql in "$@"; do args+=(-c "$sql"); done
  docker compose exec -T "$BENCH_DB_SERVICE" \
    psql -U "$BENCH_PG_USER" -d "$BENCH_PG_ADMIN_DB" -q -v ON_ERROR_STOP=1 "${args[@]}"
}
