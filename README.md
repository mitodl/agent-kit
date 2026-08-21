# agent-kit

A shared toolkit of AI agent utilities for the team. At its centre are the
three witan packages — [`witan-council`](./mcp/servers/witan/README.md),
[`witan-code`](./mcp/servers/witan-code/README.md), and
[`witan-core`](./packages/witan-core/README.md), the shared memory,
work-coordination, and code-graph layer for coding agents — published to PyPI
alongside [`agent-config-kit`](./packages/agent-config-kit/README.md) and the
[`ol-agent-kit`](./packages/agent-kit/README.md) meta-package, for five in
all. The repo also holds reusable skills, custom agent definitions, MCP server
install helpers, and sample configurations.

Documentation for the witan packages lives in [`docs/`](./docs/) — tutorials,
guides, a generated reference for every MCP tool, CLI command and environment
variable, and the architecture notes. Build it locally with `just docs-serve`.
It is set up to publish on Read the Docs as **witan-context** once that project
is registered.

## Repository Structure

```
.
├── mcp/servers/witan/         # witan-council — memory, tasks, workflow; the `witan` umbrella CLI
├── mcp/servers/witan-code/    # witan-code — tree-sitter code graph + cross-repo bridge
├── packages/witan-core/       # witan-core — shared internals for the two servers above
├── packages/agent-config-kit/ # agent-kit — the CLI that installs the skills/MCP servers declared below
├── packages/agent-kit/        # PyPI meta-package (ol-agent-kit): agent-config-kit[cli] + witan + witan-code
├── skills/                    # Reusable skills, installed via agent-config-kit
├── custom-agents/             # Custom agent definitions for GitHub Copilot and Claude Code
├── mcp/                       # Install helpers and configuration for other MCP servers
├── configs/                   # Sample / reference agent configurations
├── docker/                    # Deployment images: the witan MCP tier and the omnigraph data tier
├── docs/                      # The witan-context documentation site (Zensical → Read the Docs)
├── bin/                       # Repo maintenance scripts (version/pin checks, docs generation)
└── agent-config.toml          # This repo's own manifest for agent-kit
```

## Quick Start

Setup for this repo's own MCP servers and skills is driven by
[`agent-config-kit`](./packages/agent-config-kit/README.md) (`agent-kit`) and the
manifest at [`agent-config.toml`](./agent-config.toml) — a single declarative
file that installs MCP servers, skills, and hooks into whichever coding-agent
platforms it detects (Claude Code, Pi, GitHub Copilot, OpenCode, ...).

### Installing `agent-kit`

```bash
uv tool install 'agent-config-kit[cli]'
```

To also pull in the [`witan`](./mcp/servers/witan/README.md) and
[`witan-code`](./mcp/servers/witan-code/README.md) MCP servers in one shot,
install the [`ol-agent-kit`](./packages/agent-kit/README.md) meta-package
instead — it depends on all three and carries no code of its own (`agent-kit`
was already taken on PyPI, hence the `ol-` prefix on this one; the console
script is still `agent-kit` either way):

```bash
uv tool install ol-agent-kit
```

### Applying this repo's manifest

```bash
agent-kit apply agent-config.toml                   # everything, every detected platform
agent-kit apply agent-config.toml --profile python  # just the python profile (+ universal)
agent-kit apply agent-config.toml --dry-run         # preview without writing
agent-kit validate agent-config.toml                # check for drift
agent-kit profiles agent-config.toml                # list profiles + entry counts
```

`agent-config.toml`'s `[profiles.*]` tables mirror the `skills/` category
layout — pick the profile matching your specialty (`python`,
`infrastructure`, `containers`, `dagster`, `process`, ...) to install the
`universal` baseline plus just the skills relevant to your work. Selecting no
profile installs the whole catalog, including the `toolhive-swe` MCP
servers below — they aren't part of any profile, so a `--profile` run
skips them.

Run `agent-kit apply agent-config.toml --scope project` instead of the default
`global` scope to register servers/skills in the current project only
rather than user-wide.

This registers the skill catalog plus the [`toolhive-swe`](./mcp/servers/toolhive-swe/README.md)
remote MCP server (one entry per environment tier). It does **not** register
[`witan`](./mcp/servers/witan/README.md) or [`witan-code`](./mcp/servers/witan-code/README.md) —
those own their own registration lifecycle via `witan setup` (see their
READMEs), so they aren't duplicated in this manifest.

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

`agent-kit` isn't specific to this repo — any project or team can write its own
`agent-config.toml` manifest to declare MCP servers, skills, and hooks, then
run `agent-kit apply` to install them into whichever coding-agent platforms are
detected locally. For local, personal use, point `[skills]` at paths on
disk; for team use, point at a shared repo (this one, or your own) via a
`git+https://...` source so everyone applies the same manifest:

```toml
[skills]
my-local-skill = "skills/my-local-skill/SKILL.md"
team-skill     = "git+https://github.com/mitodl/agent-kit.git#subdirectory=skills/process/dependency-updates/SKILL.md"
```

See [`packages/agent-config-kit/README.md`](./packages/agent-config-kit/README.md)
for the full manifest format, `apply`/`validate`/`profiles` command
reference, and how remote (`https://`/`git+`) skill and hook sources are
fetched and cached.

## Contributing

1. Add new skills under `skills/<category>/<skill-name>/` — follow the [skill authoring guide](./skills/README.md) or use the [`creating-skills`](./skills/workflow/creating-skills/SKILL.md) skill.
2. Register the skill in [`agent-config.toml`](./agent-config.toml)'s `[skills]` table (and add it to the relevant `[profiles.*]` entry) so `agent-kit apply` picks it up.
3. Add new custom agents under `custom-agents/<platform>/` — follow the [agent authoring guide](./custom-agents/README.md).
4. Add MCP install helpers under `mcp/servers/<server-name>/` — follow the [MCP guide](./mcp/README.md).
5. Open a PR with a brief description of what the addition does and why it's useful.

## License

[BSD-3-Clause](./LICENSE)
