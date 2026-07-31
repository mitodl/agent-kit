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

## Still to do: publish pipeline (ol-infrastructure)

The `witan` and `omnigraph` Pulumi stacks provision their own ECR repos
(`witan-<env>`, `omnigraph-server-<env>` respectively) and references `:latest`, following
kubewatch_webhook_handler's "ECR repo in Pulumi, image built separately by
Concourse" split. The Concourse image-build job that builds these two
Dockerfiles and pushes to those repos — plus the Pulumi deploy pipelines for
the two tiers, gated on the image builds — still needs to be written in
ol-infrastructure (task
`tk-concourse-image-build-pulumi-deploy-pipelines-fo-5bef89`). Because the
build context is this repo, wire that job's `agent-kit` git resource `paths:`
filter to `docker/**`, `pyproject.toml`, `uv.lock`, `packages/**`, and
`mcp/servers/witan{,-code}/**` so a change to either image triggers a rebuild.
