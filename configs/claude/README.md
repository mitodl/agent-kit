# Claude Code Configuration Samples

Ready-to-use configuration snippets for Claude Code and Claude Desktop.

## Files

| File | Purpose |
|------|---------|
| *(none yet)* | |

## Useful Snippets

### `claude_desktop_config.json` skeleton

Location: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `%APPDATA%\Claude\claude_desktop_config.json` (Windows)

```json
{
  "mcpServers": {
    "my-server": {
      "command": "npx",
      "args": ["-y", "@my-org/mcp-server"],
      "env": {}
    }
  }
}
```

### `CLAUDE.md` skeleton

```markdown
# Project Context

<brief description of the project for Claude>

## Key Conventions

- ...

## Common Commands

- Build: `...`
- Test: `...`
- Lint: `...`
```

## Resources

- [Claude Desktop MCP setup](https://docs.anthropic.com/en/docs/claude-code/mcp)
- [CLAUDE.md memory docs](https://docs.anthropic.com/en/docs/claude-code/memory)
