# agent-kit

A shared toolkit of AI agent utilities for the team, including reusable skills,
custom agent definitions, MCP server install helpers, and sample configurations.

## Repository Structure

```
.
├── skills/          # Reusable skills compatible with the Vercel skills CLI (npx skills)
├── custom-agents/   # Custom agent definitions for GitHub Copilot and Claude Code
├── mcp/             # Install helpers and configuration for common MCP servers
└── configs/         # Sample / reference agent configurations
```

## Quick Start

### Installing skills

Install all skills from this repo into your project:

```bash
npx skills add mitodl/agent-kit
```

Install a specific skill:

```bash
npx skills add mitodl/agent-kit --skill uv-python-workflow
```

Install globally (user-level, available in all projects):

```bash
npx skills add mitodl/agent-kit --global
```

Browse available skills without installing:

```bash
npx skills add mitodl/agent-kit --list
```

See [`skills/`](./skills/README.md) for the full skill catalog.

### Setting up an MCP server

See [`mcp/`](./mcp/README.md) for per-server install scripts and configuration snippets.

### Using a custom agent

See [`custom-agents/`](./custom-agents/README.md) for agent definitions and setup
instructions for GitHub Copilot and Claude Code.

## Contributing

1. Add new skills under `skills/<category>/<skill-name>/` — follow the [skill authoring guide](./skills/README.md) or use the [`creating-skills`](./skills/workflow/creating-skills/SKILL.md) skill.
2. Add new custom agents under `custom-agents/<platform>/` — follow the [agent authoring guide](./custom-agents/README.md).
3. Add MCP install helpers under `mcp/servers/<server-name>/` — follow the [MCP guide](./mcp/README.md).
4. Open a PR with a brief description of what the addition does and why it's useful.

## License

[BSD-3-Clause](./LICENSE)
