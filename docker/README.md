# witan deployment images

Container images for running witan as a shared, multi-user service
(WorkflowProject `wp-witan-multi-user-service-deployment`, ADR-0009 in
ol-infrastructure). Two images make up the deployment:

| Image | Dockerfile | What it runs |
| --- | --- | --- |
| `witan` | [`witan.Dockerfile`](./witan.Dockerfile) | The MCP tier — `witan serve --transport streamable-http`, hosted behind the ToolHive operator. |
| `omnigraph-server` | [`omnigraph-server.Dockerfile`](./omnigraph-server.Dockerfile) | The data tier — the S3-backed omnigraph graph server the MCP tier talks to over the cluster network. |

Both are **built from the repository root** (the build context must reach the
whole uv workspace and `mcp/servers/witan/schema/schema.pg`):

```bash
GIT_TAG=$(git rev-parse --short HEAD)
docker build -f docker/witan.Dockerfile            -t witan:${GIT_TAG} .
docker build -f docker/omnigraph-server.Dockerfile -t omnigraph-server:${GIT_TAG} .
```

## `witan` image

- Installs the `witan` umbrella CLI plus `witan-code` from the top-level uv
  workspace (`uv sync --frozen --all-packages`), so `witan code …` and the
  `code_*` tools are available alongside `memory_*` / `task_*` / `workflow_*`.
- Bakes the pinned `omnigraph` **client** binary on `PATH`. witan's
  `OmnigraphClient` shells out to it for every graph call (local *and* remote)
  and constructs one at import time, so the binary must be present or the
  server will not start. Baking it in also removes the `witan setup` download
  from the container's cold-start path.
- `ENTRYPOINT ["witan"]`; the default `CMD` is the deployed invocation
  (`serve --transport streamable-http --host 0.0.0.0 --port 8000`). The
  `witan` stack's `MCPServer` overrides `args`; env (`WITAN_OIDC_ISSUER`,
  `WITAN_OIDC_AUDIENCE`, `WITAN_ACTOR_TOKENS_FILE`, `WITAN_MEMORY_URI`,
  `WITAN_MEMORY_TOKEN`) is supplied by the stack.
- Installs `git`. witan-code shells out to it to resolve a checkout's repo URI,
  branch, and root (`witan_code/repo.py`), and the CI indexer below clones with
  it.
- Also carries [`witan-ci-index`](./witan-ci-index.sh), the **CI code-graph
  indexer** — see below. Same image, different entrypoint.

### `witan-ci-index` (CI code-graph indexer)

Every repo's shared code graph has exactly one writer entitled to its default
(`main`) view, and this script is it: witan-code refuses that write, and the
stale-file purge that goes with it, from any process that has not declared
`WITAN_CODE_INDEX_ROLE=ci`. Run as a Kubernetes CronJob by ol-infrastructure's
`applications/witan/ci_indexer.py`, overriding this image's entrypoint.

It sweeps `WITAN_CODE_CI_REPOS` (whitespace-separated canonical repo URIs),
shallow-cloning each repo's default branch in turn and running
`witan code index .` against the cluster graph. A repo that fails to clone or
index does not abort the sweep; the script exits non-zero if any did.

It runs **in-cluster** rather than in GitHub Actions because omnigraph-server
is ClusterIP-only and deliberately has no HTTPRoute — outside the cluster a
code graph is reachable only through the MCP tier, at one round trip per store
operation, and a full-repo index makes thousands of them. Shipping it in the
`witan` image rather than its own keeps the process writing a shared graph on
the same build as the one serving it.

Required env: `WITAN_CODE_CI_REPOS`, `WITAN_CODE_SERVER` (the omnigraph-server
base URL), `WITAN_CODE_TOKEN` (the `svc-witan-ci` bearer token). The last two
are required rather than defaulted because witan-code's response to a missing
server is to index into a local `.omni` directory and report success — inside
a pod whose filesystem is then discarded, while the shared graph goes stale.
`WITAN_CODE_CI_ALLOW_LOCAL_STORE=1` waives them for testing the script itself.

#### Cloning repos (GitHub App)

Public repos clone anonymously and need no credential. To authenticate clones,
set all three of `WITAN_CODE_GITHUB_APP_ID`,
`WITAN_CODE_GITHUB_APP_INSTALLATION_ID`, and `WITAN_CODE_GITHUB_APP_KEY_FILE`
(a mounted PEM). A partial set is an error, not a fallback to anonymous — the
latter fails only on private repos, which reads like a GitHub-side permissions
problem rather than a missing mount.

A GitHub App rather than a deploy key or a PAT: a deploy key is scoped to one
repository, so a fleet sweep would need one per repo; a PAT is long-lived and
carries everything its owner can read. An App issues tokens that expire in an
hour and needs only `contents: read` to clone plus `metadata: read` to answer
the visibility check below.

#### Private repos are refused

A shared code graph grants `read` to every actor holding a bearer token, so a
private repo indexed into one is readable by every witan user. The sweep
therefore asks GitHub whether each repo is private **before** cloning it and
refuses the private ones, counting the refusal as a failure so it reaches the
job summary. A visibility question that cannot be answered (a 404, or GitHub
unreachable) refuses too.

`WITAN_CODE_CI_ALLOW_PRIVATE_REPOS=1` waives it. That is the explicit opt-in,
not the fix — the fix is per-repo read scoping, designed in
`mcp/servers/witan/docs/adr/0010-private-code-graph-read-scoping.md` and not
built yet.

Do not read the App's *installation* as a second control here. As installed on
mitodl it is `repository_selection: "all"`, so it already reaches every private
repo in the org and adding one to `managed_repos` is the only step there is.
The refusal above is what makes that step visible.

The token is minted **per repo**, not per sweep: it lasts an hour and a cold
run is allowed three, so a single token would expire partway through the first
real run. It reaches git through a credential helper reading the environment,
never through the clone URL — git echoes remote URLs in its error messages, so
a URL-embedded credential would print itself into the job log on the first
failed clone.

`WITAN_CODE_GITHUB_API_URL` overrides the API host (GitHub Enterprise, or a
stub in tests). See `witan_code/github_app.py`.

## `omnigraph-server` image

- Bakes **both** upstream release binaries (`omnigraph-server` to serve,
  `omnigraph` for the boot-time cluster convergence), checksum-verified against
  the release `.sha256`.
- Bakes `schema.pg` at `/etc/omnigraph/cluster/schema.pg`. The `omnigraph`
  stack mounts its generated `cluster.yaml` alongside it via a ConfigMap
  `subPath` (single-file overlay) so it does not shadow the baked-in schema.
- **Auto-bootstraps the cluster on start**
  ([`omnigraph_server_entrypoint.py`](./omnigraph_server_entrypoint.py)):
  renders policy group membership, runs `omnigraph cluster apply`, then
  `exec`s the server. This is the remote analogue of witan's local
  `_ensure_graph` auto-bootstrap — it is what creates the cluster's declared
  graph(s) under the S3 storage root and applies their schemas, so `Pulumi up`
  alone produces a *queryable* graph with no manual runbook step. (Graph names
  are set in the deployment's `cluster.yaml`, not in this image; the
  convention is to name them after the owning package — `council`, `code` —
  overridable per env.) A direct apply on an empty root creates the ledger,
  and on a converged root it is a no-op.
- **Hands the cluster lock over on every restart.** From omnigraph 0.12 on,
  the server, `cluster apply`, `optimize` and `cleanup` each leave an
  exclusive lock in the storage root when they exit, and the next writer is
  refused until it is force-unlocked. The entrypoint unlocks the previous
  server's `serve` lock and its own apply's `deployment` lock, and only after
  it holds the unlock Lease and the Kubernetes API shows no other live server
  pod. It refuses to start (exit 1) on a `graph_operation` lock, on an
  interrupted apply, and on a `cluster.yaml` that drops a graph (0.13 deletes
  a dropped graph's storage). The module docstring has the sequence;
  [the 0.13 upgrade spec](../docs/internals/design/omnigraph-0-13-upgrade-spec.md)
  (D3, D4) has the reasoning. On 0.14 a server that shut down cleanly leaves
  no lock, so the unlock only runs after a crash or a kill; the boot's direct
  apply still refuses any held lock, and CLI writers still keep theirs.
- **Speaks the 0.13 and the 0.14 CLI.** The scripts read `omnigraph version
  --json` (0.13 has no `--json`) and pick the argument shapes: 0.13's
  `--config <dir>` and `--cluster <root>`, or 0.14's `--config <dir> --target
  <root>` and `--target <root>`, where the root is `cluster.yaml`'s `storage`.
  0.14's `cluster status` reports no lock operation, so the 0.14 path reads
  the lock from `cluster plan`. A stock 0.14 CLI refuses an inline `s3://`
  target; the image needs the CLI patched with
  [ModernRelay/omnigraph#1189](https://github.com/ModernRelay/omnigraph/pull/1189),
  which takes the region from `AWS_REGION` or `AWS_DEFAULT_REGION` and refuses
  `AWS_PROFILE` without an explicit endpoint. See
  [the 0.14 upgrade spec](../docs/internals/design/omnigraph-0-14-upgrade-spec.md) (D3).
- **Carries the two commands that run beside the server**, for the
  `omnigraph` stack to use as a Job's `command`:
  - [`omnigraph_plan_gate.py`](./omnigraph_plan_gate.py) runs the
    entrypoint's plan check and nothing else. As a pre-deploy Job it fails a
    deploy on a schema or policy change the next boot would refuse, while the
    old pod is still serving. It reads the entrypoint's
    `OMNIGRAPH_SERVER_BEARER_TOKENS_FILE`, `OMNIGRAPH_APPLY_ACTOR` and
    `OMNIGRAPH_GRAPH_REMOVAL_ALLOWLIST`, and needs no Kubernetes access.
  - [`omnigraph_maintenance.py`](./omnigraph_maintenance.py) is the nightly
    maintenance window. `optimize` and `cleanup` are refused while the server
    holds its lock, so it scales the server Deployment to 0, takes over the
    `serve` lock, maintains each graph in `cluster.yaml` (every `optimize`
    before any `cleanup`, whose pass starts one graph further along each
    cleanup night), and restores the replica count. It also restores on
    SIGTERM. The module docstring has the cases where it leaves the server
    down on purpose.
- `ENTRYPOINT` is the bootstrap wrapper; the default `CMD`
  (`--cluster /etc/omnigraph/cluster --bind 0.0.0.0:8080`) matches the stack's
  `args`.

The entrypoint reads these from the environment:

| Variable | Required | Purpose |
|---|---|---|
| `OMNIGRAPH_SERVER_BEARER_TOKENS_FILE` | with policy bundles | Actor-token map the group membership is rendered from (also read by the server). |
| `OMNIGRAPH_APPLY_ACTOR` | with policy bundles | Actor passed to `cluster plan`/`apply` as `--as`. With policy installed, omnigraph refuses a schema change that names no actor. |
| `OMNIGRAPH_UNLOCK_LEASE` | in Kubernetes | Name of the pre-created `coordination.k8s.io` Lease every force-unlocker takes. The ServiceAccount needs `get` and `update` on it. |
| `OMNIGRAPH_POD_SELECTOR` | in Kubernetes | Label selector of the server Deployment's pods. The ServiceAccount needs `list` on pods. |
| `POD_NAME` | no | This pod's name. Defaults to `HOSTNAME`. |
| `OMNIGRAPH_PEER_WAIT_SECONDS` | no | How long to wait for another server pod to exit before giving up (default 50). |
| `OMNIGRAPH_GRAPH_REMOVAL_ALLOWLIST` | no | Comma-separated graph ids that `cluster.yaml` is allowed to drop, which deletes them. |
| `OMNIGRAPH_UNLOCK_WITHOUT_PEERS` | outside Kubernetes | `1` allows force-unlock with no Lease or pod check. Ignored in Kubernetes. |

The maintenance window reads `OMNIGRAPH_UNLOCK_LEASE` and `POD_NAME` as above,
and these. It takes the pod selector from the Deployment itself, and its
ServiceAccount needs `list` on pods too.

| Variable | Required | Purpose |
|---|---|---|
| `OMNIGRAPH_SERVER_DEPLOYMENT` | yes | Name of the server Deployment. The ServiceAccount needs `get` on it and `get` and `update` on its `scale`. |
| `OMNIGRAPH_WINDOW_SECONDS` | yes | How long after the start a new command may still begin. The Job's `activeDeadlineSeconds` has to be longer by one command's timeout and the restore. |
| `OMNIGRAPH_CLEANUP_OLDER_THAN` | yes | Retention passed to `cleanup --older-than`, e.g. `30d`. |
| `OMNIGRAPH_CLEANUP_ISO_WEEKDAY` | no | Comma-separated UTC days `cleanup` also runs on (1 is Monday, 7 is Sunday), e.g. `7` for Sundays or `1,2,3,4,5,6,7` for every night. Unset or empty, it never runs. A day outside 1-7 or listed twice fails the run before the server is stopped. |
| `OMNIGRAPH_COMMAND_TIMEOUT_SECONDS` | no | Longest one `optimize` or `cleanup` may run before it is stopped (default 600). |
| `OMNIGRAPH_SERVER_STOP_SECONDS` | no | How long to wait for the server pods to exit after the scale-down (default 90). |

Its pod needs `terminationGracePeriodSeconds: 120`. After SIGTERM it stops the
running command (up to 30s), removes that command's lock and scales the server
back up, and on the default 30s it is killed before the scale-up. It exits 0
only when every graph was maintained and the server is back (or left down for
a migration).

The Deployment carries the annotation `ol.mit.edu/migration-armed` while a
storage-format migration is armed. The window skips its run when it sees it,
and does not scale the server back up if it appears during the window.

Outside Kubernetes (no `KUBERNETES_SERVICE_HOST`) the Lease and the pod check
are skipped, and the entrypoint refuses to force-unlock unless
`OMNIGRAPH_UNLOCK_WITHOUT_PEERS=1` is set. `cluster apply` always leaves a lock,
so a local `docker run` needs it even on an empty root. Set it only when this
container is the only writer of the cluster root.

## Base image / glibc

Both images use **Debian trixie** bases (`debian:trixie-slim`,
`python:3.14-slim-trixie`). The upstream omnigraph release binaries require
glibc ≥ 2.39; the older bookworm bases (`debian:12-slim`, `python:*-slim`,
glibc 2.36) cannot run them.

## Version pin

The omnigraph version is pinned in three places, and they are one decision
spelled three times:

| Where | What it governs |
|---|---|
| `packages/witan-core/witan_core/omnigraph_install.py :: _OMNIGRAPH_VERSION` | what `witan setup` puts on a developer's PATH |
| `docker/witan.Dockerfile :: ARG OMNIGRAPH_VERSION` | the CLI baked into the MCP tier image |
| `docker/omnigraph-server.Dockerfile :: ARG OMNIGRAPH_VERSION` | the deployed data tier |

They **must** agree. omnigraph uses strict single-version storage: a binary
refuses a graph written by a different on-disk format, in either direction, so
a client and a server on different releases cannot share a store at all.

Renovate's custom manager (`renovate.json`) bumps all three together, and
`just check-omnigraph-pins` fails CI if they drift. Both exist because the
manager silently covered only the installer for a full release cycle while
this file claimed otherwise, and nothing caught it.

## Publish pipeline (ol-infrastructure)

Both images are built and deployed by Concourse pipelines in ol-infrastructure:
`src/ol_concourse/pipelines/infrastructure/omnigraph/pipeline.py` and
`.../witan/pipeline.py` (pipeline sets `ol-infrastructure-pulumi-omnigraph` and
`ol-infrastructure-pulumi-witan`). Each builds its Dockerfile here, pushes to
ECR, then runs the matching Pulumi stack gated on that build.

The **ECR repository is created by the build job**, idempotently on every push
— not by Pulumi. There is one repo per image (`witan`, `omnigraph-server`, no
`-<env>` suffix), shared across CI/QA/Production in the same AWS account, since
a single repo cannot be owned by three independent per-env stacks. The Pulumi
stacks only *consume* it, pinning the Deployment image by digest from
`{WITAN,OMNIGRAPH}_DOCKER_SHA` (or `_DOCKER_TAG`), supplied by the build job
via the pulumi-provisioner's `env_vars_from_files`. Pinning by digest is what
makes a new push actually change the pod spec and trigger a rollout. There is
no `:latest` fallback: with neither variable set the stack raises
`Either <APP>_DOCKER_TAG or <APP>_DOCKER_SHA must be set` rather than deploying
something unpinned.

Because the build context is this repo, each pipeline's `agent-kit` git
resource already filters `paths:` to `docker/`, `pyproject.toml`, `uv.lock`,
`packages/`, `mcp/servers/witan/`, and `mcp/servers/witan-code/`, so a change
to either image triggers a rebuild.
