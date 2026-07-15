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
[`docs/design/witan-core-extraction-spec.md`](../../docs/design/witan-core-extraction-spec.md).

## Invariant

`witan_core` imports **neither** `witan` nor `witan_code`. It is a leaf below
both, preserving the one-directional `witan` → `witan_code` optional-mount DAG
(`witan` mounts `witan-code` as `witan code`; `witan-code` never imports `witan`).

## Dependencies

The base package is stdlib-only. Heavier concerns are gated behind extras so
neither server pulls weight it doesn't use:

- `witan-core[cli]` → `cyclopts`, `rich` (CLI scaffolding, styled installer output)
- `witan-core[mcp]` → `fastmcp` (MCP elicitation primitives)

## Status

Scaffold only. The incremental extraction tasks (see the epic) land modules here
and delete the now-duplicated copies from each server.
