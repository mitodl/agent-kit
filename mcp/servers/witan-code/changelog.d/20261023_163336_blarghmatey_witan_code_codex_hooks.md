### Fixed

- `inject-context` gives Codex guidance for tools Codex has. `witan-code setup` registers one bare `UserPromptSubmit` hook for Claude Code and Codex, so Codex got Claude's block, which says to load the `code_*` tools with `ToolSearch` and to run `/witan-code`; Codex has neither. Without `--client`, the command now reads the hook's stdin payload, and a Codex payload (it carries `turn_id`) gets Codex's `tool_search` call and a pointer to the `witan-code` skill. Any other payload, or none, gets the Claude block as before. `--client codex` selects the Codex block explicitly.
