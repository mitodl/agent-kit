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

#### Cloning private repos (GitHub App)

Public repos clone anonymously and need no credential. For private ones, set
all three of `WITAN_CODE_GITHUB_APP_ID`, `WITAN_CODE_GITHUB_APP_INSTALLATION_ID`,
and `WITAN_CODE_GITHUB_APP_KEY_FILE` (a mounted PEM). A partial set is an
error, not a fallback to anonymous — the latter fails only on private repos,
which reads like a GitHub-side permissions problem rather than a missing mount.

A GitHub App rather than a deploy key or a PAT: a deploy key is scoped to one
repository, so a fleet sweep would need one per repo; a PAT is long-lived and
carries everything its owner can read. An App issues tokens that expire in an
hour, needs only `contents: read`, and its *installation* is the list of repos
it can reach — managed by an org admin in GitHub, so a private repo cannot be
pulled into a shared graph by a Pulumi config change alone.

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
  ([`omnigraph-server-entrypoint.sh`](./omnigraph-server-entrypoint.sh)):
  `omnigraph cluster import` (first boot) or `cluster refresh` (state already
  exists in the storage backend) followed by `cluster apply`, then `exec`s the
  server. This is the remote analogue of witan's local `_ensure_graph`
  auto-bootstrap — it is what creates the cluster's declared graph(s) under the
  S3 storage root and applies their schemas, so `Pulumi up` alone produces a
  *queryable* graph with no manual runbook step. (Graph names are set in the
  deployment's `cluster.yaml`, not in this image; the convention is to name
  them after the owning package — `council`, `code` — overridable per env.) The sequence is idempotent: `apply` is a
  no-op once converged, and the import→refresh split keeps pod restarts (whose
  local `__cluster/` working state is ephemeral) reconciling with the existing
  storage-backed ledger instead of erroring.
- `ENTRYPOINT` is the bootstrap wrapper; the default `CMD`
  (`--cluster /etc/omnigraph/cluster --bind 0.0.0.0:8080`) matches the stack's
  `args`.

## Base image / glibc

Both images use **Debian trixie** bases (`debian:trixie-slim`,
`python:3.14-slim-trixie`). The upstream omnigraph release binaries require
glibc ≥ 2.39; the older bookworm bases (`debian:12-slim`, `python:*-slim`,
glibc 2.36) cannot run them.

## Version pin

`OMNIGRAPH_VERSION` (default `0.8.1`) **must** match witan_core's installer pin
(`packages/witan-core/witan_core/omnigraph_install.py :: _OMNIGRAPH_VERSION`).
The same Renovate custom-manager pin drives both — bump them together.

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
