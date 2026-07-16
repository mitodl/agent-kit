# Changelog

All notable changes to `witan-core` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [0.1.0] - 2026-07-15

Initial release of the shared `witan-core` package (import `witan_core`),
mirroring `agent-config-kit`. Both witan MCP servers now depend on it via a
`[tool.uv.sources]` editable path plus a `witan-core>=0.1,<1` PyPI range. This
establishes the package and the deliberate reversal of the "no cross-package
import" convention. See `docs/design/witan-core-extraction-spec.md`.

### Added

- `popen_detached` (`witan_core._detach`) — cross-platform detached subprocess spawning.
- `install_omnigraph` (`witan_core.omnigraph_install`) — the pinned omnigraph
  binary installer and single source of `_OMNIGRAPH_VERSION` (`rich` imported lazily).
- `confirm`/`text` (`witan_core.elicit`) — MCP elicitation primitives (the `mcp` extra).
- `normalise`/`find_git_config` (`witan_core.repo_key`) — the cross-layer
  repo-key canonicalizer, with a golden contract test.
- `now_iso` (`witan_core.timeutil`).
- Throttled-optimize mechanics (`witan_core.maintenance`) — interval parsing,
  atomic last-run stamp, and due-check.
- `OmnigraphClient` base (`witan_core.omnigraph`) — the omnigraph-CLI subprocess
  wrapper (write lock, retry/repair, per-actor admission-cap backoff,
  `OmnigraphConflict`); each server subclasses it for its own tail.

Extracted from the duplicated surface of `witan` (witan-council) and
`witan-code`; the now-duplicated copies were deleted from both servers.
