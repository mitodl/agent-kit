# AGENTS.md

## Project Overview

A shared toolkit of AI agent utilities for the mitodl team: reusable skills,
custom agent definitions, MCP server configurations, and sample agent configs.
The primary artifact is a catalog of `SKILL.md` files, plus MCP server
registrations, installed declaratively via the
[`agent-kit`](./packages/agent-kit/README.md) CLI (built on the
[`agent-config-kit`](./packages/agent-config-kit/README.md) library) and
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
  agent-config-kit/      # Cross-agent MCP/skill/hook registration library (no CLI)
  agent-kit/             # The agent-kit CLI, bundled with witan + witan-code
configs/         # Sample / reference agent configurations
docs/            # Design docs and implementation specs
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
| `uv tool install agent-kit` | Install the `agent-kit` CLI |
| `agent-kit apply agent-config.toml` | Install all MCP servers/skills into every detected platform |
| `agent-kit apply agent-config.toml --profile <name>` | Install just one profile's skills (+ `universal`) |
| `agent-kit validate agent-config.toml` | Check for drift between the manifest and on-disk config |
| `agent-kit profiles agent-config.toml` | List profiles and their resolved entry counts |
| `prek run --all-files` | Run all pre-commit checks |
| `cd mcp/servers/witan && uv run --group test pytest` | Run witan MCP server tests |
| `cd mcp/servers/witan-code && uv run --group test pytest` | Run witan-code MCP server tests |

CI runs on push/PR: skill ZIP packaging (on tags) and witan server tests.

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
- Skills are distributed as ZIPs on GitHub releases (tagged `v*`) — the publish workflow handles this automatically.
- Each publishable package (`agent-config-kit`, `mcp/servers/witan`, `mcp/servers/witan-code`, `packages/agent-kit`) carries a `[tool.bumpversion]` config — bump a release with `cd <package-dir> && uvx bump-my-version@1.4.1 bump patch|minor|major`, then commit and push to `main`; each package's `publish-*.yml` workflow tests, builds, publishes to PyPI, and tags the release automatically whenever its `pyproject.toml` version line changes. `packages/agent-kit`'s `dependencies` on `agent-config-kit`/`witan-council`/`witan-code` are open-ended floors (no upper bound), so a new release of any of the three is picked up by a fresh install without `agent-kit` itself needing a release.

## Further Reading

- [`README.md`](./README.md) — Quick Start for installing/applying `agent-kit`
- [`packages/agent-config-kit/README.md`](./packages/agent-config-kit/README.md) — `agent-kit` manifest schema and command reference
- [`skills/README.md`](./skills/README.md) — full skill catalog with descriptions
- [`mcp/README.md`](./mcp/README.md) — MCP server structure and available servers
- [`mcp/servers/witan/README.md`](./mcp/servers/witan/README.md) — witan graph-memory server
- [`custom-agents/README.md`](./custom-agents/README.md) — agent definitions for Claude/Copilot
- [`docs/`](./docs/) — design docs and implementation specs
