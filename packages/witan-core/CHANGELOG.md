# Changelog

All notable changes to `witan-core` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

## [0.1.0] - 2026-07-15

### Added

- Initial scaffold of the shared `witan-core` package (import `witan_core`),
  mirroring `agent-config-kit`. Both witan MCP servers now depend on it via a
  `[tool.uv.sources]` editable path plus a `witan-core>=0.1,<1` PyPI range. No
  surface is extracted yet — this establishes the package and the reversal of the
  "no cross-package import" convention. See
  `docs/design/witan-core-extraction-spec.md`.
