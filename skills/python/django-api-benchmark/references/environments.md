# Execution environments

Every step has to run in the application's Django, reach Postgres with
administrative rights, and notice when a `git checkout` takes effect. Those
three are the only things that differ between dev environments, so they are
the only pluggable part. The seed, the measurement method and the analysis are
identical everywhere.

**Which backend to use is a property of the developer's machine, not of the
project or the benchmark**, which is why it lives in the uncommitted
`benchmarks/benchmark.local.toml`. Do not commit it, and do not edit someone
else's.

## Choosing one

Probe in this order, and **ask rather than guess if more than one hits** —
having both a per-repo compose stack and the local-dev cluster on one machine
is normal:

1. Nothing containerised, or the app runs in the same environment you are
   typing in → `local` (the default; no local config file needed).
2. A compose file in the repo, and `docker compose ps` shows its services up →
   `compose`.
3. `kubectl config get-contexts -o name` includes `local-dev`, and the cluster
   has a namespace for this app → `k8s`.

## `local`

```toml
[backend]
kind = "local"
```

Subprocesses of the harness itself. A `git checkout` is visible immediately, so
there is nothing to wait for. Administrative SQL goes through `psycopg` or
`psycopg2` if either is importable, otherwise the `psql` binary.

This is the default, so a project whose `benchmark.toml` points at a reachable
Postgres needs no local config at all.

## `compose`

```toml
[backend]
kind = "compose"
service = "web"
db_service = "db"

[database]
url_template = "postgres://postgres:postgres@db:5432/{name}"
admin_url = "postgres://postgres:postgres@db:5432/postgres"
```

Each step gets a fresh throwaway container (`docker compose run --rm --no-deps
-T`), which makes this the cleanest of the three measurement environments.

Nothing here starts services — `--no-deps` is deliberate, so a stray dependency
does not come up mid-measurement. Run `docker compose up -d` first.

Note that `url_template` is the DSN **as the container resolves it**: the
compose service name, not `localhost`.

## `k8s` — ol-infrastructure's `local-dev`

```toml
[backend]
kind = "k8s"
context = "local-dev"            # required; never inherited
namespace = "mit-learn"
selector = "app=mitlearn-webapp"
container = "app"                # the web pod also runs an nginx sidecar
db_selector = "cnpg.io/cluster=local-pg"
settle_seconds = 10

[database]
url_template = "postgres://app:localdev@local-pg-rw.local-infra.svc.cluster.local:5432/{name}"
admin_url = "postgres://app:localdev@local-pg-rw.local-infra.svc.cluster.local:5432/postgres"
```

`context` is **required and is passed on every command**. A developer's current
`kubectl` context is routinely a deployed cluster, the documented local-dev
commands carry no `--context` of their own, and this harness runs `DROP
DATABASE`. There is no inherit-the-ambient-context path.

Postgres is one shared CloudNativePG cluster in `local-infra` serving every
app, and it is **not published to the host** — no port mapping, no standing
port-forward. That is why `admin_url` is a cluster-internal DSN and why
`db_selector` points administrative SQL at the Postgres pod. No superuser
secret is needed: the `app` role is granted `CREATEDB` at bootstrap.

Namespaces and selectors for the apps on local-dev:

| App | `namespace` | deployment |
| --- | --- | --- |
| mit-learn | `mit-learn` | `mitlearn-webapp` |
| learn-ai | `learn-ai` | `learnai-webapp` |
| mitxonline | `mitxonline` | `mitxonline-webapp` |
| odl-video-service | `odl-video-service` | `odlvideo-webapp` |

### Why a ref switch needs waiting for

Tilt's live update is **push-based and one-way**: it tars changed files and
streams them into the running container. A host-side `git checkout` therefore
does not take effect synchronously, and if the two refs differ in `uv.lock`,
Tilt runs `uv sync --frozen` inside the container first. Starting an arm before
that finishes benchmarks the wrong ref, silently.

The runner handles this without needing to know any of it: it takes up to three
`.py` files that genuinely differ between the two refs and polls the
application environment until its copy of each matches the host's. That proves
the running code *is* the target ref, rather than proving only that some sync
happened. `settle_seconds` then covers granian's re-import — it runs with
`--reload` and goes unready while Django reloads, and an arm measured during
that re-import is competing with it for CPU. Raise it if the app is slow to
come back.

If the two refs differ in no Python file, the runner says so rather than
silently passing an unverifiable check.

### Fidelity differences worth stating in the report

- The benchmark **shares the pod** with granian and the nginx sidecar rather
  than getting a fresh container. There are no CPU limits (`requests: 100m`,
  memory limit 2Gi), so no CFS throttling, but there is real contention. Scale
  the app's celery worker deployments to 0 for the run.
- granian's reloader **watches `/src`**. The harness writes nothing to the
  source tree — results travel over stdout, never through a shared filesystem —
  but do not point `--out-dir` inside it either.
- Postgres is a **single 512Mi instance shared** by `mitlearn`, `learnai`,
  `mitxonline`, `odlvideo`, `keycloak` and `litellm`. That is a different cache
  regime from a dedicated compose database, and other apps' traffic is real
  contention.

None of this invalidates a result, but it widens the spread. Lean harder on the
two checks that matter most: the delta must exceed the run-to-run variability,
and it must hold at a second seed shape.

## Safety

The harness runs `DROP DATABASE`. Three things stand between that and a real
database:

- **The scratch database name must start with `bench`.** The name is derived as
  `bench_<benchmark name>` unless overridden, and a name without the prefix is
  refused at load time. In a shared cluster this is what rules out a typo
  landing on `mitlearn`.
- **`admin_url` may not point at the scratch database itself**, which is
  checked at load time.
- **The `k8s` backend never inherits the ambient kubectl context.** It is
  required configuration and is passed explicitly on every command.

`config.resolved.json` records the backend and the redacted DSNs alongside the
numbers, so what was measured against is on the record.
