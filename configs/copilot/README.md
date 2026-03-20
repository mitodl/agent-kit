# GitHub Copilot Configuration Samples

Ready-to-use configuration snippets for GitHub Copilot.

## Files

| File | Purpose |
|------|---------|
| *(none yet)* | |

## Useful Snippets

### `.vscode/settings.json` — enable MCP servers for Copilot Chat

```json
{
  "github.copilot.chat.mcp.enabled": true
}
```

### `.vscode/mcp.json` — define MCP servers available to Copilot Chat

```json
{
  "servers": {
    "my-server": {
      "command": "npx",
      "args": ["-y", "@my-org/mcp-server"]
    }
  }
}
```

## Resources

- [Copilot settings reference](https://docs.github.com/en/copilot/reference/vs-code-settings)
- [MCP in VS Code](https://code.visualstudio.com/docs/copilot/chat/mcp-servers)
