# MCP Servers

Install helpers, configuration snippets, and setup notes for common
[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers.

## Structure

```
mcp/
└── servers/
    └── <server-name>/
        ├── README.md        # What the server does, prerequisites
        ├── install.sh       # Optional: automated install / setup script
        └── config/
            ├── claude.json  # Snippet for claude_desktop_config.json
            └── copilot.json # Snippet for .vscode/mcp.json / copilot config
```

## Adding a Server

1. Create `mcp/servers/<server-name>/README.md` describing the server.
2. Add a `config/` directory with ready-to-paste config snippets for each platform.
3. Optionally add an `install.sh` for any non-trivial setup steps.

## Available Servers

| Server | Description |
|--------|-------------|
| [`witan`](servers/witan/README.md) | Team-wide shared knowledge graph: memory, workflow projects, task tracking, and the umbrella CLI. Uses `witan serve` to mount both memory and code tools in one MCP entry. |
| [`witan-code`](servers/witan-code/README.md) | Tree-sitter code graph (Layer 2): indexes repo symbols and their relationships. Can run standalone or be mounted automatically by `witan serve`. |
| [`toolhive-swe`](servers/toolhive-swe/README.md) | Remote, hosted ToolHive SWE MCP server, one installation per environment tier (ci/qa/production) each at its own hostname (Streamable HTTP + Keycloak OAuth). No local process — just config + browser auth. |

## Resources

- [MCP specification](https://modelcontextprotocol.io/docs)
- [Anthropic MCP servers](https://github.com/modelcontextprotocol/servers)
- [VS Code MCP support](https://code.visualstudio.com/docs/copilot/chat/mcp-servers)
