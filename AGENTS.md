# AGENTS.md

## Project Overview

A shared toolkit of AI agent utilities for the mitodl team: reusable skills,
custom agent definitions, MCP server configurations, and sample agent configs.
The primary artifact is a catalog of `SKILL.md` files installable via `npx skills`.

## Repository Layout

```
skills/          # Reusable skills (SKILL.md per skill), organized by category
  python/        # uv, cyclopts CLI conventions
  dagster/       # dg-based code location structure
  infrastructure/# Pulumi IaC, Vault K8s auth
  containers/    # Docker image builds with uv
  workflow/      # validate-before-commit, skill authoring
  process/       # GitHub issues/PRs/RFCs, standup, dependency management
custom-agents/   # Agent definitions for Claude Code and GitHub Copilot
mcp/             # MCP server install helpers and config snippets
  servers/witan/         # Graph-structured memory MCP server (Python)
  servers/witan-code/    # Tree-sitter code-graph MCP server (Python)
  servers/grafana-cloud/ # Grafana Cloud MCP config
configs/         # Sample / reference agent configurations
docs/            # Design docs and implementation specs
```

## Dev Setup

No build step for skills — they are plain Markdown. For the `witan` MCP servers:

```bash
cd mcp/servers/witan
uv sync
```

Install pre-commit hooks (uses `prek`):

```bash
prek install
```

## Key Commands

| Command | Purpose |
|---------|---------|
| `npx skills add mitodl/agent-kit` | Install all skills into current project |
| `npx skills add mitodl/agent-kit --skill <name>` | Install a specific skill |
| `npx skills add mitodl/agent-kit --list` | Browse skills without installing |
| `prek run --all-files` | Run all pre-commit checks |
| `cd mcp/servers/witan && uv run pytest` | Run witan MCP server tests |

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
3. Open a PR.

See [`skills/workflow/creating-skills/SKILL.md`](./skills/workflow/creating-skills/SKILL.md) for the full authoring guide.

## Conventions & Gotchas

- Skill `name` in frontmatter must exactly match the directory name.
- `description` is what the agent reads to decide when to load the skill — make it trigger-rich, not just a label.
- MD013 (line length) and MD033 (inline HTML) are disabled in markdownlint; long lines in code blocks are fine.
- `witan` MCP servers use `uv` exclusively — never `pip` directly.
- Skills are distributed as ZIPs on GitHub releases (tagged `v*`) — the publish workflow handles this automatically.

## Further Reading

- [`skills/README.md`](./skills/README.md) — full skill catalog with descriptions
- [`mcp/README.md`](./mcp/README.md) — MCP server structure and available servers
- [`mcp/servers/witan/README.md`](./mcp/servers/witan/README.md) — witan graph-memory server
- [`custom-agents/README.md`](./custom-agents/README.md) — agent definitions for Claude/Copilot
- [`docs/`](./docs/) — design docs and implementation specs
