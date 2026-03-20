# Custom Agents

Custom agent definitions for use with AI coding assistants.

## Platforms

| Directory | Platform |
|-----------|----------|
| [`copilot/`](./copilot/README.md) | GitHub Copilot (Copilot Extensions / custom instructions) |
| [`claude/`](./claude/README.md) | Claude Code (custom sub-agents / `CLAUDE.md` fragments) |

## Authoring an Agent

Each agent lives in `custom-agents/<platform>/<agent-name>/` and should include:

- **`agent.md`** — the agent's system prompt / instruction file
- **`README.md`** — description, intended use, and any platform-specific setup steps

See the platform subdirectories for format details and examples.
