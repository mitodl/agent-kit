# Workflow Skills

Cross-cutting process conventions that apply regardless of tool or language.

| Skill | Description |
| ----- | ----------- |
| [`validate-before-commit`](./validate-before-commit/SKILL.md) | Run the hooks (`prek`) → `mypy` → `pulumi preview` proactively before declaring done |
| [`creating-skills`](./creating-skills/SKILL.md) | Create a new skill: frontmatter, category placement, progressive disclosure, index updates |
| [`extract-style-profile`](./extract-style-profile/SKILL.md) | Build an evidence-backed coding/communication style profile for a person, team, or repo from git and GitHub history, split into pre-AI baseline and AI-era drift |
| [`verify-before-claiming`](./verify-before-claiming/SKILL.md) | Re-fetch live state before any status claim, check other worktrees before calling work missing, label findings verified or inferred. Shell rules that hold in bash, zsh or fish: multi-step logic in a script file, source edits through the edit tool, CRLF stripped before parsing, real values in commands handed to the user. In context every session: a `session_start` hook on Claude Code and Codex (trust it once with `/hooks` in the Codex TUI), a plugin on Pi. Copilot and OpenCode get the skill only, which loads when a request matches |

Witan-specific workflow skills (`witan-memory`, `witan-project-tracker`,
`witan-task`, `witan-workflow`) are distributed via the witan MCP server package
at `mcp/servers/witan/witan/skills/` and installed by `witan setup`.
