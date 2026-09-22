# Execution environments

The harness has to run `manage.py`, reach Postgres, and make a `git switch` take
effect in whatever is actually executing the app. Those three things are the only
parts that differ between dev environments, so they are the only parts that are
pluggable. Everything else - the seed, the measurement method, the analysis - is
identical.

`run.sh` sources `.bench/backend.sh`. Copy one of the templates in
[`backends/`](backends/) there during setup; the invocation stays exactly
`.bench/run.sh [baseline-ref]` either way.

## The contract

A backend defines two functions and two variables:

| Member | Meaning |
| --- | --- |
| `bench_exec VAR=VAL ... -- <cmd>` | Run `<cmd>` in the app's Django environment with those variables set. Stdin must be forwarded (steps are fed as `manage.py shell < script.py`) and stdout returned verbatim |
| `bench_psql <sql> [<sql> ...]` | Run admin SQL against the app's Postgres, connected to some database other than `$BENCH_DB` - it is about to be dropped |
| `BENCH_DB_URL` | The scratch DSN **as the app process resolves it**, which is rarely what the host would use |
| `BENCH_TARGET_DESC` | One line naming what is about to be written to. Printed before the destructive step and quoted in the report |

Optionally `bench_wait_settled`, called after a ref change once the new files are
confirmed present, to block until the app environment has finished reacting.
`run.sh` defaults it to a no-op.

`BENCH_EXTRA_ENV` is a space-separated `VAR=VAL` list in both backends; each one
translates it to its own syntax.

**Results never travel through a shared filesystem.** `seed.py` prints
`SEED_SHAPE`, `bench.py` prints `BENCH_RESULT`, `trace.py` prints `TRACE_RESULT`;
`run.sh` captures each on stdout and threads the seeded ids back in as
`BENCH_IDS_JSON`. A host-visible bind mount is a property of one backend, not
something the harness may assume.

## Choosing one

Probe in this order, and **ask rather than guess if more than one hits** - having
both a per-repo compose stack and the local-dev cluster on one machine is normal:

1. A compose file in the repo, and `docker compose ps` shows its services up →
   `compose.sh`.
2. `kubectl config get-contexts -o name` includes `local-dev`, and the cluster has
   a namespace for this app → `k8s-tilt.sh`.

## `compose.sh` - the repo's own docker compose stack

`docker compose run --rm --no-deps -T … web` for each step, `docker compose exec
-T db psql -U postgres` for admin SQL, scratch DSN
`postgres://postgres:postgres@db:5432/$BENCH_DB`. Knobs: `BENCH_SERVICE`
(default `web`), `BENCH_DB_SERVICE` (default `db`), `BENCH_PG_USER`,
`BENCH_PG_PASSWORD`, `BENCH_PG_ADMIN_DB`.

Nothing here starts services - `--no-deps` is deliberate, so a stray dependency
does not come up mid-measurement. `docker compose up` first.

The repo is bind-mounted, so a host-side `git switch` changes what the container
runs immediately, and each step gets a fresh container with nothing else running
in it. This is the cleaner of the two measurement environments.

## `k8s-tilt.sh` - ol-infrastructure's `local-dev`

k3d + Tilt + Kubernetes. Steps run as `kubectl exec -i -n <ns> deploy/<app> -c app
-- env …`; the `-c app` is required because the web pod also runs an nginx
sidecar. The working directory is already `/src`.

Postgres is one shared CloudNativePG cluster in `local-infra` serving every app.
It is **not published to the host** - no port mapping, no standing port-forward -
so admin SQL goes through `kubectl exec … local-pg-1 -c postgres -- psql -U app`.
No superuser secret is needed: the `app` role is granted `CREATEDB` at bootstrap.
The app-visible DSN is
`postgres://app:localdev@local-pg-rw.local-infra.svc.cluster.local:5432/$BENCH_DB`.

Namespace and deployment default to mit-learn's; the other apps:

| App | `BENCH_NAMESPACE` | `BENCH_DEPLOYMENT` |
| --- | --- | --- |
| mit-learn | `mit-learn` | `mitlearn-webapp` |
| learn-ai | `learn-ai` | `learnai-webapp` |
| mitxonline | `mitxonline` | `mitxonline-webapp` |
| odl-video-service | `odl-video-service` | `odlvideo-webapp` |

### Why the ref switch needs waiting for

Tilt's live update is **push-based and one-way**: it tars changed files and
streams them into the running container. A host-side `git switch` therefore does
not take effect synchronously, and if the two refs differ in `uv.lock`, Tilt runs
`uv sync --frozen` inside the container first. Starting an arm before that
finishes benchmarks the wrong ref, silently.

`run.sh`'s `bench_sync_ref` handles this without needing to know any of it: it
takes up to three `.py` files that genuinely differ between the two refs and polls
`bench_exec -- cat <file>` until the container's copy matches the host's. That
proves the running code *is* the target ref, rather than proving only that some
sync happened. The k8s backend then adds `bench_wait_settled`, which waits for the
Deployment to report `Available` again - granian runs `--reload` and goes unready
while it re-imports Django, and an arm measured during that re-import is competing
with it for CPU.

### Fidelity differences worth stating in the report

- The benchmark **shares the pod** with granian and the nginx sidecar, rather than
  getting a fresh isolated container. There are no CPU limits on the container
  (`requests: 100m`, memory limit 2Gi), so there is no CFS throttling, but there
  is contention. Scale the app's celery worker deployments to 0 for the run.
- granian's reloader **watches `/src`**, ignoring only `frontends` and
  `staticfiles`. Anything the harness writes under the source tree triggers a
  Django re-import mid-measurement. This is why `BENCH_IDS_PATH` defaults to
  `/tmp` and why nothing writes to `.bench/` from inside the container.
- Postgres is a **single 512Mi instance shared** by `mitlearn`, `learnai`,
  `mitxonline`, `odlvideo`, `keycloak` and `litellm` - a different cache regime
  from a dedicated per-repo compose database, and other apps' traffic is real
  contention.

None of this invalidates the result, but it widens the spread. Lean harder on the
two checks the skill already requires: the delta must exceed the run-to-run
spread, and it must hold at a second seed shape.

## Safety

The harness runs `DROP DATABASE`, so both backends are guarded:

- **`run.sh` refuses any `BENCH_DB` not starting with `bench`.** That also rules
  out every real database in a shared cluster, where a typo would otherwise land
  on `mitlearn`.
- **The k8s backend never inherits the ambient kubectl context.** It always passes
  `--context "${BENCH_KUBE_CONTEXT:-local-dev}"`, refuses a context name matching
  `ci|qa|prod|applications`, and refuses a context that does not exist. A
  developer's current context is routinely a real remote cluster, and the
  documented local-dev commands carry no `--context` of their own.
- **`BENCH_TARGET_DESC` is printed before the drop** and again above the results,
  so what was measured against is on the record.
