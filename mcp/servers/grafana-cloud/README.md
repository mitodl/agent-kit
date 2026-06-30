# grafana-cloud

The [Grafana Cloud MCP server](https://grafana.com/docs/grafana-cloud/machine-learning/assistant/configure/cloud-mcp/)
— a **remote, hosted** server that gives coding agents access to your Grafana
Cloud stack (dashboards, datasources, Prometheus/Loki queries, alerts, incidents,
and more) through the Model Context Protocol.

Unlike [`witan`](../witan/README.md) and [`witan-code`](../witan-code/README.md),
there is nothing to build, package, or run locally. You only wire a config entry
into your agent and authenticate through a browser.

| | |
|---|---|
| **Endpoint** | `https://mcp.grafana.com/mcp` |
| **Transport** | Streamable HTTP only (no stdio, no SSE) |
| **Auth** | OAuth 2.1 — browser consent flow on first connect (no token/API key to store) |
| **Optional header** | `X-Grafana-URL: https://<your-stack>.grafana.net` |

The `X-Grafana-URL` header pins each MCP server entry to **one** Grafana stack.
We run three stacks, so each is registered as its own named server (each with
its own OAuth consent):

| Server name | Stack | `X-Grafana-URL` |
|---|---|---|
| `grafana-ci`   | mitolci         | `https://mitolci.grafana.net` |
| `grafana-qa`   | mitolqa         | `https://mitolqa.grafana.net` |
| `grafana-prod` | mitolproduction | `https://mitolproduction.grafana.net` |

## Quick Start

**Claude Code (automated):**

```bash
# From this directory — registers all three stacks at USER scope, so they're
# available from any repo / directory you work in (not just this project):
./install.sh --agent claude

# Or just one stack:
./install.sh --agent claude --instance prod   # ci | qa | prod

# Share via a project-local .mcp.json instead of your user config:
./install.sh --agent claude --scope project   # user (default) | project | local

# Or directly with the Claude CLI (one per stack). `--scope user` makes it
# global; omit it and the server is only registered for the current project.
# Note: name + URL come before --header (--header is variadic and will
# otherwise swallow the URL).
claude mcp add grafana-prod https://mcp.grafana.com/mcp \
    --scope user \
    --transport http \
    --header "X-Grafana-URL: https://mitolproduction.grafana.net"
```

On first use of each server, Claude opens a browser for the Grafana OAuth
consent flow (once per stack). Run `/mcp` inside Claude Code (or
`claude mcp list`) to confirm the connections and authenticate.

**VS Code / GitHub Copilot:** copy [`config/copilot.json`](./config/copilot.json)
into `.vscode/mcp.json` (or your user `mcp.json`). It already lists all three stacks.

**pi:** copy [`config/pi.json`](./config/pi.json) into `~/.pi/agent/mcp.json`.

**Claude Desktop:** merge [`config/claude.json`](./config/claude.json) into your
`claude_desktop_config.json`.

> Trim any stacks you don't need from the JSON (or use `--instance` with the
> installer) — there's no harm in registering all three, but each adds tools
> and a separate OAuth prompt.

> Remote MCP over Streamable HTTP with OAuth requires a recent agent version.
> If your client doesn't support remote HTTP transport, upgrade it first.

## Prerequisites

- A Grafana Cloud account with access to the stack you want to query.
- An MCP client that supports remote Streamable HTTP transport and OAuth
  (Claude Code, Claude Desktop, VS Code/Copilot, Cursor, Windsurf, …).

## Notes

- No secrets are stored in this repo or in your config — the snippets contain
  only the public endpoint and your (non-secret) stack URL. Authentication is
  handled entirely by the OAuth flow.
- The installer registers at **user** scope by default (global to your account,
  usable from any directory). Use `--scope project` to share via a project-local
  `.mcp.json`, or `--scope local` for the old project-private behavior.
- To remove from Claude Code, match the scope you installed at:
  `claude mcp remove grafana-ci -s user` (and `-qa` / `-prod`).
