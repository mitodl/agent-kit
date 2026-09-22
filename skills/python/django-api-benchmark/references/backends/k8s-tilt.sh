# TEMPLATE - execution backend: ol-infrastructure's `local-dev`
# (k3d + Tilt + Kubernetes). Copy to .bench/backend.sh.
#
# Supplies the contract described in references/environments.md. Defaults are
# for mit-learn; see the table below for the other apps in the stack.
#
#   app                 BENCH_NAMESPACE     BENCH_DEPLOYMENT
#   mit-learn           mit-learn           mitlearn-webapp
#   learn-ai            learn-ai            learnai-webapp
#   mitxonline          mitxonline          mitxonline-webapp
#   odl-video-service   odl-video-service   odlvideo-webapp

BENCH_KUBE_CONTEXT=${BENCH_KUBE_CONTEXT:-local-dev}
BENCH_NAMESPACE=${BENCH_NAMESPACE:-mit-learn}
BENCH_DEPLOYMENT=${BENCH_DEPLOYMENT:-mitlearn-webapp}
# The web pod runs granian AND an nginx sidecar, so the container is not optional.
BENCH_CONTAINER=${BENCH_CONTAINER:-app}

# One shared CloudNativePG cluster hosts every app's database. The `app` role
# is granted CREATEDB at bootstrap, so no superuser secret is needed.
BENCH_PG_NAMESPACE=${BENCH_PG_NAMESPACE:-local-infra}
BENCH_PG_POD=${BENCH_PG_POD:-local-pg-1}
BENCH_PG_USER=${BENCH_PG_USER:-app}
BENCH_PG_PASSWORD=${BENCH_PG_PASSWORD:-localdev}
BENCH_PG_ADMIN_DB=${BENCH_PG_ADMIN_DB:-app}
BENCH_PG_HOST=${BENCH_PG_HOST:-local-pg-rw.local-infra.svc.cluster.local}

# --- safety: never inherit the ambient kubectl context -----------------------
# This harness runs DROP DATABASE. A developer's current context is routinely a
# real remote cluster, and every command below would silently follow it.
case "$BENCH_KUBE_CONTEXT" in
  *ci*|*qa*|*prod*|*applications*)
    echo "refusing: BENCH_KUBE_CONTEXT='${BENCH_KUBE_CONTEXT}' looks like a deployed cluster" >&2
    exit 1
    ;;
esac
if ! kubectl config get-contexts -o name | grep -qxF "$BENCH_KUBE_CONTEXT"; then
  echo "no kubectl context named '${BENCH_KUBE_CONTEXT}'; is local-dev up?" >&2
  exit 1
fi

KUBECTL=(kubectl --context "$BENCH_KUBE_CONTEXT")

# As resolved inside the app container: in-cluster DNS, not a host port. The
# local-dev Postgres is not published to the host at all.
BENCH_DB_URL="postgres://${BENCH_PG_USER}:${BENCH_PG_PASSWORD}@${BENCH_PG_HOST}:5432/${BENCH_DB}"
BENCH_TARGET_DESC="local-dev k8s: context '${BENCH_KUBE_CONTEXT}', ${BENCH_NAMESPACE}/${BENCH_DEPLOYMENT}, database '${BENCH_DB}' on ${BENCH_PG_HOST}"

# Unlike compose's `run --rm`, this execs a new process INSIDE the running web
# pod, alongside granian and the nginx sidecar. See environments.md for what
# that costs in measurement fidelity.
bench_exec() {
  local envs=() kv
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
    envs+=("$1")
    shift
  done
  shift  # the --
  for kv in ${BENCH_EXTRA_ENV:-}; do envs+=("$kv"); done
  # -i but not -t: stdin is forwarded, and no TTY means no \r in the output.
  # workingDir is already /src, so `python manage.py` resolves.
  "${KUBECTL[@]}" exec -i -n "$BENCH_NAMESPACE" "deploy/${BENCH_DEPLOYMENT}" \
    -c "$BENCH_CONTAINER" -- env "${envs[@]}" "$@"
}

bench_psql() {
  local args=() sql
  for sql in "$@"; do args+=(-c "$sql"); done
  "${KUBECTL[@]}" exec -i -n "$BENCH_PG_NAMESPACE" "$BENCH_PG_POD" -c postgres \
    -- psql -U "$BENCH_PG_USER" -d "$BENCH_PG_ADMIN_DB" -q -v ON_ERROR_STOP=1 "${args[@]}"
}

# Tilt's live sync is push-based and asynchronous, and granian runs --reload:
# after a `git switch` the pod re-imports Django, and its readiness probe goes
# unready while that happens. Wait for it to settle BEFORE the generic
# content check in run.sh, so the measurement does not race a reload that is
# burning CPU in the same pod.
bench_wait_settled() {
  # Give granian a moment to notice the synced files and drop out of Ready,
  # then wait for the Deployment to become Available again. Keyed on the
  # Deployment rather than a pod label selector so it needs no knowledge of
  # how the app labels its pods.
  sleep "${BENCH_SETTLE_SECONDS:-5}"
  "${KUBECTL[@]}" wait --for=condition=Available \
    "deployment/${BENCH_DEPLOYMENT}" -n "$BENCH_NAMESPACE" \
    --timeout="${BENCH_SYNC_TIMEOUT:-300}s" >/dev/null
}
