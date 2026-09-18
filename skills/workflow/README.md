# Workflow Skills

Cross-cutting process conventions that apply regardless of tool or language.

| Skill | Description |
| ----- | ----------- |
| [`validate-before-commit`](./validate-before-commit/SKILL.md) | Run `pre-commit` → `mypy` → `pulumi preview` proactively before declaring done |
| [`creating-skills`](./creating-skills/SKILL.md) | Create a new skill: frontmatter, category placement, progressive disclosure, index updates |
| [`extract-style-profile`](./extract-style-profile/SKILL.md) | Build an evidence-backed coding/communication style profile for a person, team, or repo from git and GitHub history, split into pre-AI baseline and AI-era drift |

Witan-specific workflow skills (`witan-memory`, `witan-project-tracker`,
`witan-task`, `witan-workflow`) are distributed via the witan MCP server package
at `mcp/servers/witan/witan/skills/` and installed by `witan setup`.
