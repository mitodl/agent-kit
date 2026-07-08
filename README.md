# agent-kit

A shared toolkit of AI agent utilities for the team, including reusable skills,
custom agent definitions, MCP server install helpers, and sample configurations.

## Repository Structure

```
.
├── skills/                    # Reusable skills, installed via agent-config-kit
├── custom-agents/             # Custom agent definitions for GitHub Copilot and Claude Code
├── mcp/                       # Install helpers and configuration for common MCP servers
├── configs/                   # Sample / reference agent configurations
├── packages/agent-config-kit/ # ac-kit — the CLI that installs everything above
└── agent-config.toml          # This repo's own manifest for ac-kit
```

## Quick Start

Setup for this repo's own MCP servers and skills is driven by
[`agent-config-kit`](./packages/agent-config-kit/README.md) (`ac-kit`) and the
manifest at [`agent-config.toml`](./agent-config.toml) — a single declarative
file that installs MCP servers, skills, and hooks into whichever coding-agent
platforms it detects (Claude Code, Pi, GitHub Copilot, OpenCode, ...).

### Installing `ac-kit`

```bash
uv tool install 'agent-config-kit[cli]'
```

### Applying this repo's manifest

```bash
ac-kit apply agent-config.toml                   # everything, every detected platform
ac-kit apply agent-config.toml --profile python  # just the python profile (+ universal)
ac-kit apply agent-config.toml --dry-run         # preview without writing
ac-kit validate agent-config.toml                # check for drift
ac-kit profiles agent-config.toml                # list profiles + entry counts
```

`agent-config.toml`'s `[profiles.*]` tables mirror the `skills/` category
layout — pick the profile matching your specialty (`python`,
`infrastructure`, `containers`, `dagster`, `process`, ...) to install the
`universal` baseline plus just the skills relevant to your work. Selecting no
profile installs the whole catalog.

Run `ac-kit apply --scope project` instead of the default `global` scope to
register servers/skills in the current project only rather than user-wide.

This registers the [`witan`](./mcp/servers/witan/README.md) and
[`witan-code`](./mcp/servers/witan-code/README.md) MCP servers —
team-wide shared memory/task tracking and a tree-sitter code graph,
respectively — alongside the skill catalog.

See [`skills/`](./skills/README.md) for the full skill catalog and
[`packages/agent-config-kit/README.md`](./packages/agent-config-kit/README.md)
for the full manifest schema, remote skill/hook sources, and profile
composition.

### Setting up an MCP server

See [`mcp/`](./mcp/README.md) for per-server install scripts and configuration
snippets, including [`witan`](./mcp/servers/witan/README.md) and
[`witan-code`](./mcp/servers/witan-code/README.md).

### Using a custom agent

See [`custom-agents/`](./custom-agents/README.md) for agent definitions and setup
instructions for GitHub Copilot and Claude Code.

## Using `agent-config-kit` for your own project or team skills

`ac-kit` isn't specific to this repo — any project or team can write its own
`agent-config.toml` manifest to declare MCP servers, skills, and hooks, then
run `ac-kit apply` to install them into whichever coding-agent platforms are
detected locally. For local, personal use, point `[skills]` at paths on
disk; for team use, point at a shared repo (this one, or your own) via a
`git+https://...` source so everyone applies the same manifest:

```toml
[skills]
my-local-skill = "skills/my-local-skill/SKILL.md"
team-skill     = "git+https://github.com/mitodl/agent-kit.git#subdirectory=skills/process/dependency-updates"
```

See [`packages/agent-config-kit/README.md`](./packages/agent-config-kit/README.md)
for the full manifest format, `apply`/`validate`/`profiles` command
reference, and how remote (`https://`/`git+`) skill and hook sources are
fetched and cached.

## Contributing

1. Add new skills under `skills/<category>/<skill-name>/` — follow the [skill authoring guide](./skills/README.md) or use the [`creating-skills`](./skills/workflow/creating-skills/SKILL.md) skill.
2. Register the skill in [`agent-config.toml`](./agent-config.toml)'s `[skills]` table (and add it to the relevant `[profiles.*]` entry) so `ac-kit apply` picks it up.
3. Add new custom agents under `custom-agents/<platform>/` — follow the [agent authoring guide](./custom-agents/README.md).
4. Add MCP install helpers under `mcp/servers/<server-name>/` — follow the [MCP guide](./mcp/README.md).
5. Open a PR with a brief description of what the addition does and why it's useful.

## License

[BSD-3-Clause](./LICENSE)
