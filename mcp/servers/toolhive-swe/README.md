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

**Claude Code (automated):**

```bash
# From this directory — registers all three tiers at USER scope, so they're
# available from any repo / directory you work in (not just this project):
./install.sh --agent claude

# Or just one tier:
./install.sh --agent claude --instance prod   # ci | qa | prod

# Share via a project-local .mcp.json instead of your user config:
./install.sh --agent claude --scope project   # user (default) | project | local

# Or directly with the Claude CLI (one per tier). `--scope user` makes it
# global; omit it and the server is only registered for the current project.
claude mcp add toolhive-swe-prod https://toolhive-swe.ol.mit.edu/mcp \
    --scope user \
    --transport http
```

On first use of each server, Claude opens a browser for the Keycloak OAuth
consent flow (once per tier). Run `/mcp` inside Claude Code (or
`claude mcp list`) to confirm the connections and authenticate.

**VS Code / GitHub Copilot:** copy [`config/copilot.json`](./config/copilot.json)
into `.vscode/mcp.json` (or your user `mcp.json`). It already lists all three tiers.

**pi:** copy [`config/pi.json`](./config/pi.json) into `~/.pi/agent/mcp.json`.

**Claude Desktop:** merge [`config/claude.json`](./config/claude.json) into your
`claude_desktop_config.json`.

> Trim any tiers you don't need from the JSON (or use `--instance` with the
> installer) — there's no harm in registering all three, but each adds a
> separate OAuth prompt and you likely only have Keycloak access to some tiers.

> Remote MCP over Streamable HTTP with OAuth requires a recent agent version.
> If your client doesn't support remote HTTP transport, upgrade it first.

## Prerequisites

- An account in the `ol-platform-engineering` Keycloak realm for **each tier**
  you want to connect to (ci / qa / production accounts are provisioned
  separately — access to one tier does not imply access to another).
- An MCP client that supports remote Streamable HTTP transport and OAuth
  (Claude Code, Claude Desktop, VS Code/Copilot, Cursor, Windsurf, …).

## Notes

- No secrets are stored in this repo or in your config — the snippets contain
  only the public per-tier endpoint URLs. Authentication is handled entirely
  by the Keycloak OAuth flow.
- The installer registers at **user** scope by default (global to your account,
  usable from any directory). Use `--scope project` to share via a project-local
  `.mcp.json`, or `--scope local` for the old project-private behavior.
- To remove from Claude Code, match the scope you installed at:
  `claude mcp remove toolhive-swe-ci -s user` (and `-qa` / `-prod`).
