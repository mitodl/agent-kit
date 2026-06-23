# Workflow Skills

Cross-cutting process conventions that apply regardless of tool or language.

| Skill | Description |
| ----- | ----------- |
| [`validate-before-commit`](./validate-before-commit/SKILL.md) | Run `pre-commit` → `mypy` → `pulumi preview` proactively before declaring done |
| [`creating-skills`](./creating-skills/SKILL.md) | Create a new skill: frontmatter, category placement, progressive disclosure, index updates |
| [`agent-memory`](./agent-memory/SKILL.md) | Read/write team shared knowledge graph; load project facts, store patterns and lessons |
| [`project-tracker`](./project-tracker/SKILL.md) | Track multi-session engineering projects; link sessions without handoffs; build workflow corpus |
| [`workflow`](./session-start/SKILL.md) | `/workflow` — interactive session linker: list active projects, pick one, call `workflow_session_start`; also `/workflow end` and `/workflow list` |
| [`task`](./task-tracker/SKILL.md) | `/task` — triage ready work, create tasks, claim tasks, and close completed graph tasks |

## Witan Skill Mirrors

The Witan workflow skills are intentionally stored in two places:

- `skills/workflow/` is the standalone agent-kit skill catalog used by
  `npx skills add`.
- `mcp/servers/witan/witan/skills/` is bundled into the Witan MCP server wheel so
  `witan setup` can install the same skills for agents that install Witan directly.

Keep the mirrored files byte-for-byte identical. The `Validate skills` GitHub
Actions workflow runs `scripts/check-witan-skills-sync.sh` to catch drift.
