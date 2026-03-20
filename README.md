# Team Agent Utilities

A shared repository of AI agent utilities for the team, including reusable skills,
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

### Installing a skill

Skills in `skills/` are compatible with the [Vercel skills CLI](https://www.npmjs.com/package/skills):

```bash
npx skills install <skill-name>
```

Or install directly from this repo:

```bash
npx skills install --registry https://github.com/<org>/agents <skill-name>
```

### Setting up an MCP server

See [`mcp/`](./mcp/README.md) for per-server install scripts and configuration snippets.

### Using a custom agent

See [`custom-agents/`](./custom-agents/README.md) for agent definitions and setup
instructions for GitHub Copilot and Claude Code.

## Contributing

1. Add new skills under `skills/<skill-name>/` — follow the [skill authoring guide](./skills/README.md).
2. Add new custom agents under `custom-agents/<platform>/` — follow the [agent authoring guide](./custom-agents/README.md).
3. Add MCP install helpers under `mcp/servers/<server-name>/` — follow the [MCP guide](./mcp/README.md).
4. Open a PR with a brief description of what the addition does and why it's useful.

## License

[BSD-3-Clause](./LICENSE)
