# Claude Code Custom Agents

Custom sub-agent definitions and `CLAUDE.md` fragments for Claude Code.

## Agent Types

- **Sub-agents** — Named agents invoked via `claude --agent <name>` or referenced
  inside a `CLAUDE.md`. Each lives in its own subdirectory with an `agent.md`.
- **`CLAUDE.md` fragments** — Reusable snippets that can be composed into a
  project's `CLAUDE.md` to give Claude persistent context.

## Structure

```
claude/
├── agents/             # Sub-agent definitions
│   └── <agent-name>/
│       ├── agent.md
│       └── README.md
└── claude-md/          # Reusable CLAUDE.md fragments
    └── <topic>/
        └── fragment.md
```

## Available Agents

| Name | Type | Description |
|------|------|-------------|
| *(none yet)* | | |

## Resources

- [Claude Code documentation](https://docs.anthropic.com/en/docs/claude-code)
- [CLAUDE.md reference](https://docs.anthropic.com/en/docs/claude-code/memory)
