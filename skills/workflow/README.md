# Workflow Skills

Cross-cutting process conventions that apply regardless of tool or language.

| Skill | Description |
| ----- | ----------- |
| [`validate-before-commit`](./validate-before-commit/SKILL.md) | Run the hooks (`prek`) → `mypy` → `pulumi preview` proactively before declaring done |
| [`creating-skills`](./creating-skills/SKILL.md) | Create a new skill: frontmatter, category placement, progressive disclosure, index updates |
| [`extract-style-profile`](./extract-style-profile/SKILL.md) | Build an evidence-backed coding/communication style profile for a person, team, or repo from git and GitHub history, split into pre-AI baseline and AI-era drift |
| [`verify-before-claiming`](./verify-before-claiming/SKILL.md) | Re-fetch live state before any status claim, check other worktrees before calling work missing, label findings verified or inferred. Shell rules that hold in bash, zsh or fish: multi-step logic in a script file, source edits through the edit tool, CRLF stripped before parsing, real values in commands handed to the user. In context every session: a `session_start` hook on Claude Code and Codex (trust it once with `/hooks` in the Codex TUI), a plugin on Pi. Copilot and OpenCode get the skill only, which loads when a request matches |

The witan-backed workflow skills are distributed via the witan MCP server
package at `mcp/servers/witan/witan/skills/` and installed by `witan setup`,
not by `agent-kit apply`:

| Skill | Description |
| ----- | ----------- |
| `scope-work` | Establish the issue, ground it in the code, and route by how much process the work deserves |
| `plan-technical-approach` | Decide how to build it: engineer's approach elicited first, alternatives scored blind to authorship, delivery chunked under a size budget |
| `write-spec` | Write the design document for work spanning several PRs, sessions or repos, in the location and shape that repo already uses |
| `write-adr` | Record a significant decision as an ADR in the repo's existing convention, scoped to the files it governs |
| `implement-plan` | Execute the approved plan chunk by chunk, honouring the ADRs that govern the changed files, confirming and recording any deviation |
| `witan-task`, `witan-workflow`, `witan-project-tracker`, `witan-memory` | The task, project, session and memory graph these four drive |

Those moved out of this directory deliberately. They stop outright
when the `task_*` and `workflow_*` tools are missing, and `agent-kit apply`
does not install witan — so shipping them in the `universal` profile handed a
`universal`-only consumer four skills that refuse to run. Everyone who
installed witan for memories already has the tools they need.
