# toolhive-swe

The ToolHive SWE MCP server — a **remote, hosted** server run per environment
tier that gives coding agents access to MIT Open Learning's software
engineering tooling through the Model Context Protocol.

Unlike [`witan`](../witan/README.md) and [`witan-code`](../witan-code/README.md),
there is nothing to build, package, or run locally. You only wire a config entry
into your agent and authenticate through a browser.

We run one installation per environment tier, each at its own hostname (no shared
endpoint or routing header):

| Server name | Tier | URL |
|---|---|---|
| `toolhive-swe-ci`   | ci         | `https://toolhive-swe.ci.ol.mit.edu/mcp` |
| `toolhive-swe-qa`   | qa         | `https://toolhive-swe.qa.ol.mit.edu/mcp` |
| `toolhive-swe-prod` | production | `https://toolhive-swe.ol.mit.edu/mcp` |

| | |
|---|---|
| **Transport** | Streamable HTTP only (no stdio, no SSE) |
| **Auth** | OAuth 2.1 via Keycloak — browser consent flow on first connect (no token/API key to store) |

## Quick Start

Registration is declarative, via [`agent-config-kit`](../../../packages/agent-config-kit/README.md)'s
`agent-kit` CLI and the [`agent-config.toml`](./agent-config.toml) manifest in this
directory — there is no install script.

```bash
# One-time: install the CLI
uv tool install 'agent-config-kit[cli]'

# From this directory — registers all three tiers, on every agent platform
# agent-kit detects on your machine (Claude Code, Pi, GitHub Copilot, OpenCode):
agent-kit apply agent-config.toml

# Preview without writing anything:
agent-kit apply agent-config.toml --dry-run

# Target just one platform:
agent-kit apply agent-config.toml --platform claude

# Check for drift against what's on disk (e.g. after a manual edit):
agent-kit validate agent-config.toml
```

`agent-kit apply` registers at **global** scope by default (available from any
repo / directory you work in, not just this project) — see the manifest's
`[options]` if you want to change that. On first use of each server, your
agent opens a browser for the Keycloak OAuth consent flow (once per tier).
For Claude Code, run `/mcp` (or `claude mcp list`) to confirm the connections
and authenticate.

**Claude Desktop** isn't one of `agent-kit`'s managed platforms (it reads a
separate `claude_desktop_config.json`, not Claude Code's `~/.claude.json`) —
merge [`config/claude.json`](./config/claude.json) into it by hand.

> Only want one or two tiers? Delete the corresponding
> `[mcp_servers.toolhive-swe-*]` table(s) from `agent-config.toml` before
> running `agent-kit apply` — each tier is a separate OAuth prompt and you
> likely only have Keycloak access to some of them.

> Remote MCP over Streamable HTTP with OAuth requires a recent agent version.
> If your client doesn't support remote HTTP transport, upgrade it first.

## Prerequisites

- An account in the `ol-platform-engineering` Keycloak realm for **each tier**
  you want to connect to (ci / qa / production accounts are provisioned
  separately — access to one tier does not imply access to another).
- An MCP client that supports remote Streamable HTTP transport and OAuth
  (Claude Code, Claude Desktop, VS Code/Copilot, Cursor, Windsurf, …).

## Notes

- No secrets are stored in this repo or in your config — the manifest contains
  only the public per-tier endpoint URLs. Authentication is handled entirely
  by the Keycloak OAuth flow.
- `agent-config.toml`'s `[options] scope` defaults to `global`; pass
  `--scope project` to `agent-kit apply` to share via a project-local config
  (e.g. `.mcp.json`) instead.
- To remove a server, delete its table from `agent-config.toml` and re-run
  `agent-kit apply --prune` (or remove it by hand: `claude mcp remove
  toolhive-swe-ci`, edit `~/.pi/agent/mcp.json`, etc., matching however you
  installed it).
