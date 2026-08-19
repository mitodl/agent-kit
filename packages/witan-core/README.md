# witan-core

Shared core for the two witan MCP servers — `witan` (dist `witan-council`,
`mcp/servers/witan`) and `witan-code` (`mcp/servers/witan-code`). It is the third
shared `packages/` sibling alongside
[`agent-config-kit`](../agent-config-kit/README.md), wired into both servers via
a `[tool.uv.sources]` editable path (dev/CI) plus a published PyPI version range.

## Why

The two servers were built copy-paste-and-diverge and carried an explicit
"deliberately duplicated — no cross-package import" convention. As the shared
surface grew, fixes had to be applied twice and silently drifted — including code
that is *contractually required* to stay identical (the repo-key canonicalizer
behind the cross-layer symbol join key; the pinned omnigraph binary version, kept
in lockstep by a fragile Renovate custom manager).

`witan-core` deliberately reverses that convention. The full rationale, scope,
and per-extraction contracts live in
[`docs/internals/design/witan-core-extraction-spec.md`](../../docs/internals/design/witan-core-extraction-spec.md).

## Invariant

`witan_core` imports **neither** `witan` nor `witan_code`. It is a leaf below
both, preserving the one-directional `witan` → `witan_code` optional-mount DAG
(`witan` mounts `witan-code` as `witan code`; `witan-code` never imports `witan`).

## Dependencies

The base package is stdlib-only. Heavier concerns are gated behind extras so
neither server pulls weight it doesn't use:

- `witan-core[cli]` → `cyclopts`, `rich` (CLI scaffolding, styled installer output)
- `witan-core[mcp]` → `fastmcp` (MCP elicitation primitives)

## What's here

Extracted so far (each deletes the duplicated copies from both servers):

- `_detach.popen_detached` — cross-platform detached subprocess spawning
- `omnigraph_install` — the pinned-omnigraph-binary installer (single source of
  the version; `rich` imported lazily)
- `elicit` — the `confirm`/`text` MCP elicitation primitives (needs the `mcp`
  extra; not re-exported from the package root)
- `repo_key` — `normalise` + `find_git_config`, the cross-layer repo-key
  canonicalizer, with a golden contract test
- `timeutil.now_iso`
- `maintenance` — the throttled-optimize stamp/interval/due mechanics
- `omnigraph.OmnigraphClient` — the omnigraph-CLI subprocess wrapper base
  (write lock, retry/repair, admission-cap backoff); each server subclasses it
  (witan adds `apply_schema`; witan-code adds branch ops + bulk `load`)
- `config_file.load_toml` — shared config.toml loading (`WITAN_CONFIG` env
  var). Both servers read the same file, so one `[targets.<name>]` block can
  override both at once.
- `target_config` — the `[targets.<name>]` match/select logic: `match_target`
  (priority `match_paths` > `match_repos` > `match_hosts` > `match_orgs`),
  `parse_target_tables`, `to_list`, `local_project_path`. Each server keeps
  its own typed target model (different override fields — witan's
  `server`/`graph`/`token`/…, witan-code's `code_dir`) and calls into this
  shared matcher, which is structurally typed over just the four `match_*`
  lists.

Still local to each server (intentionally): the CLI scaffolding — its
extraction coordinates with the in-flight multi-user deployment work (see the
spec).
