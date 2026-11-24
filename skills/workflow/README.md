# Workflow Skills

Cross-cutting process conventions that apply regardless of tool or language.

| Skill | Description |
| ----- | ----------- |
| [`validate-before-commit`](./validate-before-commit/SKILL.md) | Run the hooks (`prek`) → `mypy` → `pulumi preview` proactively before declaring done |
| [`creating-skills`](./creating-skills/SKILL.md) | Create a new skill: frontmatter, category placement, progressive disclosure, index updates |
| [`extract-style-profile`](./extract-style-profile/SKILL.md) | Build an evidence-backed coding/communication style profile for a person, team, or repo from git and GitHub history, split into pre-AI baseline and AI-era drift |
| [`verify-before-claiming`](./verify-before-claiming/SKILL.md) | Re-fetch live state before any status claim, check other worktrees before calling work missing, label findings verified or inferred. Shell rules that hold in bash, zsh or fish: multi-step logic in a script file, source edits through the edit tool, CRLF stripped before parsing, real values in commands handed to the user. In context every session: a `session_start` hook on Claude Code and Codex (trust it once with `/hooks` in the Codex TUI), a plugin on Pi. Copilot and OpenCode get the skill only, which loads when a request matches |
| [`session-preflight`](./session-preflight/SKILL.md) | Session-start hook that reports expired gh/Vault/kubectl credentials, an unreachable witan or Docker daemon, and a checkout far behind origin, one line per failure with its fix |
| [`lint-exceptions`](./lint-exceptions/SKILL.md) | A linter fired: satisfy the rule first, and if a suppression really is right, take the narrowest scope the tool offers, attach a reason a reader can check, and prove the scope with a deliberate second violation. Language-agnostic; per-tool spellings in [`references/ecosystems.md`](./lint-exceptions/references/ecosystems.md) |
| [`feature-loop`](./feature-loop/SKILL.md) | Plan a change, debate it with a read-only reviewer on a model you pick, usually another provider's (codex, claude or pi CLI), stop for approval, implement, debate the code, commit locally. Loads only on an explicit request. The reviewer comes from `~/.config/feature-loop/reviewers`; nothing is shipped as a default. The reviewer can read any file you can, and what it reads goes to that provider. See Setup in the skill |

Witan-specific workflow skills (`witan-memory`, `witan-project-tracker`,
`witan-task`, `witan-workflow`) are distributed via the witan MCP server package
at `mcp/servers/witan/witan/skills/` and installed by `witan setup`.
