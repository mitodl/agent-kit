# Workflow Skills

Cross-cutting process conventions that apply regardless of tool or language.

| Skill | Description |
| ----- | ----------- |
| [`validate-before-commit`](./validate-before-commit/SKILL.md) | Run the hooks (`prek`) → `mypy` → `pulumi preview` proactively before declaring done |
| [`creating-skills`](./creating-skills/SKILL.md) | Create a new skill: frontmatter, category placement, progressive disclosure, index updates |
| [`extract-style-profile`](./extract-style-profile/SKILL.md) | Build an evidence-backed coding/communication style profile for a person, team, or repo from git and GitHub history, split into pre-AI baseline and AI-era drift |

The witan-backed workflow skills are distributed via the witan MCP server
package at `mcp/servers/witan/witan/skills/` and installed by `witan setup`,
not by `agent-kit apply`:

| Skill | Description |
| ----- | ----------- |
| `scope-work` | Establish the issue, ground it in the code, and route by how much process the work deserves |
| `plan-technical-approach` | Decide how to build it: engineer's approach elicited first, alternatives scored blind to authorship, delivery chunked under a size budget |
| `write-adr` | Record a significant decision as an ADR in the repo's existing convention, scoped to the files it governs |
| `implement-plan` | Execute the approved plan chunk by chunk, honouring the ADRs that govern the changed files, confirming and recording any deviation |
| `witan-task`, `witan-workflow`, `witan-project-tracker`, `witan-memory` | The task, project, session and memory graph these four drive |

The first four moved out of this directory deliberately. They stop outright
when the `task_*` and `workflow_*` tools are missing, and `agent-kit apply`
does not install witan — so shipping them in the `universal` profile handed a
`universal`-only consumer four skills that refuse to run. Everyone who
installed witan for memories already has the tools they need.
