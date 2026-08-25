# AGENTS.md

## Project Overview

A shared toolkit of AI agent utilities for the mitodl team: reusable skills,
custom agent definitions, MCP server configurations, and sample agent configs.
The primary artifact is a catalog of `SKILL.md` files, plus MCP server
registrations, installed declaratively via
[`agent-config-kit`](./packages/agent-config-kit/README.md) (`agent-kit`) and
this repo's [`agent-config.toml`](./agent-config.toml) manifest.

## Repository Layout

```
skills/          # Reusable skills (SKILL.md per skill), organized by category
  python/        # uv, cyclopts CLI conventions
  dagster/       # dg-based code location structure
  infrastructure/ # Pulumi IaC, Vault K8s auth
  containers/    # Docker image builds with uv
  workflow/      # validate-before-commit, skill authoring
  process/       # GitHub issues/PRs/RFCs, standup, dependency management
custom-agents/   # Agent definitions for Claude Code and GitHub Copilot
mcp/             # MCP server install helpers and config snippets
  servers/witan/         # Graph-structured memory MCP server (Python)
  servers/witan-code/    # Tree-sitter code-graph MCP server (Python)
  servers/toolhive-swe/  # ToolHive SWE MCP config (per-tier: ci/qa/prod)
packages/        # Standalone, independently-versioned Python libraries
  agent-config-kit/      # Cross-agent MCP/skill/hook registration library
  agent-kit/              # PyPI meta-package (ol-agent-kit): agent-config-kit[cli] + witan + witan-code
configs/         # Sample / reference agent configurations
docs/            # The witan-context documentation site (Zensical -> Read the Docs)
  getting-started/  # Tutorials (handwritten)
  guides/           # How-to (mostly MIRRORED from the packages by bin/gen_docs.py)
  reference/        # GENERATED from live code by bin/gen_docs.py -- never edit
  explanation/      # Architecture, memory model, coordination, ADRs
  internals/        # Historical design docs and implementation specs
```

## Dev Setup

No build step for skills — they are plain Markdown. For the MCP servers:

```bash
cd mcp/servers/witan && uv sync
cd mcp/servers/witan-code && uv sync
```

Install pre-commit hooks (uses `prek`):

```bash
prek install
```

## Key Commands

| Command | Purpose |
|---------|---------|
| `uv tool install 'agent-config-kit[cli]'` | Install the `agent-kit` CLI |
| `agent-kit apply agent-config.toml` | Install all MCP servers/skills into every detected platform |
| `agent-kit apply agent-config.toml --profile <name>` | Install just one profile's skills (+ `universal`) |
| `agent-kit validate agent-config.toml` | Check for drift between the manifest and on-disk config |
| `agent-kit profiles agent-config.toml` | List profiles and their resolved entry counts |
| `prek run --all-files` | Run all pre-commit checks |
| `just test-all` (alias `just test`) | Run every workspace package's tests, each isolated, all in parallel |
| `just test-witan-core` / `test-witan-council` / `test-witan-code` / `test-agent-config-kit` / `test-ol-agent-kit` | Run one package's tests in isolation (`*args` forwards to pytest, e.g. `just test-witan-council -k merge`) |

CI runs on push/PR: skill ZIP packaging (on tags) and per-package tests.

## Adding a Skill

1. Create `skills/<category>/<skill-name>/SKILL.md` with frontmatter:

   ```yaml
   ---
   name: your-skill-name
   description: >
     Use this skill when...  (triggers + what it does; max 1024 chars)
   license: BSD-3-Clause
   metadata:
     category: <category>
   ---
   ```

2. Add the skill to its category `README.md` table and to `skills/README.md`.
3. Register it in [`agent-config.toml`](./agent-config.toml)'s `[skills]` table (and the relevant `[profiles.*]` entry) so `agent-kit apply` picks it up.
4. Open a PR.

See [`skills/workflow/creating-skills/SKILL.md`](./skills/workflow/creating-skills/SKILL.md) for the full authoring guide.

## Conventions & Gotchas

- Skill `name` in frontmatter must exactly match the directory name.
- `description` is what the agent reads to decide when to load the skill — make it trigger-rich, not just a label.
- MD013 (line length) and MD033 (inline HTML) are disabled in markdownlint; long lines in code blocks are fine.
- `witan` MCP servers use `uv` exclusively — never `pip` directly.
- This repo is one `uv` workspace with a single shared `.venv` at the root (`packages/agent-config-kit`, `packages/agent-kit`, `packages/witan-core`, `mcp/servers/witan`, `mcp/servers/witan-code`) — running `uv sync --package X` then testing package Y against the same env risks cross-contamination (Y sees X's deps, or a stale build of a sibling you just edited). Use the `just test-*` recipes (`justfile`, repo root): each runs `uv run --isolated --package <name> --group test pytest <path>` in its own throwaway venv, so results can't leak between packages. `just test-all` runs all five concurrently via just's native `[parallel]` recipe attribute.
- **Tests never touch the machine they run on.** Every package's rootdir `conftest.py` loads `testsupport/hermetic.py` (repo root), which redirects `HOME`, the XDG dirs, the witan state files and both graph stores into a throwaway directory, clears the ambient `WITAN_*` selectors, and pins the terminal width — **at import time, not in a fixture**, because importing `witan.server` creates a graph and that happens during collection, before any fixture body runs. A suite that needs one of those values sets it itself; a suite that does not must not inherit yours. The one deliberate exception is `PATH`, which keeps its entry for the real `~/.local/bin` so the omnigraph binary stays findable — a binary is a tool, not state. A `pytest_sessionfinish` hook reports anything that reached the real home anyway: a warning locally (your machine may legitimately be writing there from another session), a failure on CI, inferred from `CI` rather than set per-workflow. Override with `AGENT_KIT_STRICT_HERMETICITY=1|0`. **A new package needs its own rootdir `conftest.py`** — nothing else will supply one, and without it the suite runs against your real home.
- Skills are distributed as ZIPs on GitHub releases (tagged `v*`) — the publish workflow handles this automatically.
- Each publishable package (`agent-config-kit`, `mcp/servers/witan`, `mcp/servers/witan-code`, `packages/agent-kit`, `packages/witan-core`) carries a `[tool.bumpversion]` config — bump a release with **`just bump <package> patch|minor|major`** (repo root), then commit and push to `main`; each package's `publish-*.yml` workflow tests, builds, publishes to PyPI, and tags the release automatically whenever its `pyproject.toml` version line changes. `just bump` writes the CHANGELOG entry's version into `pyproject.toml` only if that entry already exists, so the changelog and the version cannot drift apart; it wraps the pinned `uvx bump-my-version@1.4.1` (calling that directly still works, but skips the changelog gate and the post-check). Commit `pyproject.toml`, `CHANGELOG.md` **and `uv.lock`** together — the lockfile records workspace member versions and moves with the bump. `just check-versions` asserts version/bumpversion-config/CHANGELOG agree and runs in CI on every PR touching a `pyproject.toml` or `CHANGELOG.md`. **If a change adds a `witan_core` symbol AND a caller of it in `witan`/`witan-code`, raise that server's `witan-core>=X` floor in the same change** — the workspace resolves `witan-core` by path, so the new symbol imports fine everywhere except an external `pip install`, which then fails at import rather than at use. `just check-core-floor` is what catches that: it installs each server's wheel into a clean venv with `witan-core` pinned to exactly its declared floor and imports every module, and it runs in CI on every PR touching either server or `witan-core`. `packages/agent-kit`'s `dependencies` on `agent-config-kit[cli]`/`witan-council`/`witan-code` are open-ended floors (no upper bound), so a new release of any of the three is picked up by a fresh install without `ol-agent-kit` itself needing a release.

## Further Reading

- [`README.md`](./README.md) — Quick Start for installing/applying `agent-kit`
- [`packages/agent-config-kit/README.md`](./packages/agent-config-kit/README.md) — `agent-kit` manifest schema and command reference
- [`skills/README.md`](./skills/README.md) — full skill catalog with descriptions
- [`mcp/README.md`](./mcp/README.md) — MCP server structure and available servers
- [`mcp/servers/witan/README.md`](./mcp/servers/witan/README.md) — witan graph-memory server
- [`custom-agents/README.md`](./custom-agents/README.md) — agent definitions for Claude/Copilot
- [`docs/`](./docs/) — the **witan-context** documentation site (set up to publish on Read the Docs once that project is registered). `docs/reference/` is GENERATED and `docs/guides/` is mostly MIRRORED from the packages — do not hand-edit either; run `just docs-gen` and commit. `just docs-check` gates this in CI, and `just docs-serve` previews locally. Historical specs live in `docs/internals/`.
