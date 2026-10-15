# Workflow Skills

Cross-cutting process conventions that apply regardless of tool or language.

| Skill | Description |
| ----- | ----------- |
| [`validate-before-commit`](./validate-before-commit/SKILL.md) | Run the hooks (`prek`) → `mypy` → `pulumi preview` proactively before declaring done |
| [`creating-skills`](./creating-skills/SKILL.md) | Create a new skill: frontmatter, category placement, progressive disclosure, index updates |
| [`extract-style-profile`](./extract-style-profile/SKILL.md) | Build an evidence-backed coding/communication style profile for a person, team, or repo from git and GitHub history, split into pre-AI baseline and AI-era drift |
| [`scope-work`](./scope-work/SKILL.md) | Establish the issue, ground it in the code, and route by complexity: trivial, needs a spec, or spike first |
| [`write-adr`](./write-adr/SKILL.md) | Record a significant decision as an ADR in the repo's existing convention, scoped to the files it governs |
| [`plan-technical-approach`](./plan-technical-approach/SKILL.md) | Decide how to build it: engineer's approach elicited first, alternatives scored blind to authorship, delivery chunked under a size budget |
| [`implement-plan`](./implement-plan/SKILL.md) | Execute the approved plan chunk by chunk, honouring the ADRs that govern the changed files, confirming and recording any deviation |

Witan-specific workflow skills (`witan-memory`, `witan-project-tracker`,
`witan-task`, `witan-workflow`) are distributed via the witan MCP server package
at `mcp/servers/witan/witan/skills/` and installed by `witan setup`.
