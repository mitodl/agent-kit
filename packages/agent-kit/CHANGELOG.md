# Changelog

All notable changes to `ol-agent-kit` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (pre-1.0:
a MINOR bump may include breaking changes).

`ol-agent-kit` is a meta-package: it ships no code of its own, only open-ended
**floors** on `agent-config-kit`, `witan-council` and `witan-code` — minimum
versions with no upper bound. A fresh install therefore picks up newer releases
of any of the three automatically, and a member release does **not** need an
`ol-agent-kit` release to reach users (AGENTS.md § release).

So an entry here means a floor was raised — because a member gained something
this meta-package's users are expected to have — and the substance of what
changed is in that member's own changelog.

## [0.1.3] - 2026-08-03

This file starts here. Releases 0.1.0 through 0.1.3 predate it and are
documented only in the git history — reconstructing them after the fact would
mean inventing detail nobody recorded at the time:

- `0.1.3` — remote MCP-client mode for the standalone witan-code CLI
  ([#156](https://github.com/mitodl/agent-kit/pull/156))
- `0.1.2` — FastMCP 4 parts of MCP 2026-07-28, tranche 2
  ([#151](https://github.com/mitodl/agent-kit/pull/151))
- `0.1.1` — add `[project.scripts]` so `uv tool install` works
  ([#114](https://github.com/mitodl/agent-kit/pull/114))
- `0.1.0` — initial meta-package
  ([#113](https://github.com/mitodl/agent-kit/pull/113))
