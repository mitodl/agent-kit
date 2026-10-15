# Workflow Skills

Cross-cutting process conventions that apply regardless of tool or language.

| Skill | Description |
| ----- | ----------- |
| [`validate-before-commit`](./validate-before-commit/SKILL.md) | Run the hooks (`prek`) → `mypy` → `pulumi preview` proactively before declaring done |
| [`creating-skills`](./creating-skills/SKILL.md) | Create a new skill: frontmatter, category placement, progressive disclosure, index updates |
| [`extract-style-profile`](./extract-style-profile/SKILL.md) | Build an evidence-backed coding/communication style profile for a person, team, or repo from git and GitHub history, split into pre-AI baseline and AI-era drift |
| [`verify-before-claiming`](./verify-before-claiming/SKILL.md) | Re-fetch live state before any status claim, check other worktrees before calling work missing, label findings verified or inferred. In context every session: a `session_start` hook on Claude Code and Codex (trust it once with `/hooks` in the Codex TUI), a plugin on Pi. Copilot and OpenCode get the skill only, which loads when a request matches |
| [`scope-work`](./scope-work/SKILL.md) | Establish the issue, ground it in the code, and route by complexity: trivial, needs a spec, or spike first |
| [`write-spec`](./write-spec/SKILL.md) | Durable design record in `specs/<slug>/` — `PRODUCT.md` for user-facing intent, `TECHNICAL.md` for the engineering design |
| [`plan-technical-approach`](./plan-technical-approach/SKILL.md) | Decide how to build it: engineer's approach elicited first, alternatives scored blind to authorship, delivery chunked under a size budget |
| [`implement-spec`](./implement-spec/SKILL.md) | Execute the approved plan chunk by chunk, confirming and recording any deviation from the spec |

Witan-specific workflow skills (`witan-memory`, `witan-project-tracker`,
`witan-task`, `witan-workflow`) are distributed via the witan MCP server package
at `mcp/servers/witan/witan/skills/` and installed by `witan setup`.
