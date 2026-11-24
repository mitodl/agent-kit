# witan for VS Code

The VS Code client for the witan language server. It starts `witan lsp`, shows the ready work for the open repo, and launches an agent on a task or project in a terminal. Everything witan decides (what is ready, what to run, what to claim) comes from the server, so this extension holds no witan logic of its own ([ADR 0012](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/adr/0012-editor-integration-architecture.md)).

## Requirements

The `witan` CLI with its `lsp` extra on the PATH VS Code sees (in a WSL or SSH window, on the remote side):

```bash
uv tool install ol-agent-kit
```

`ol-agent-kit` brings witan-council and witan-code with their language-server plugins. With only witan-code installed, set `witan.server.command` to `["witan-code", "lsp"]` and you get the code-graph features without the task list.

Run `witan login` first if your work routes to a deployment; the server never prompts.

## What it does

- Hover, diagnostics, CodeLens, definition and references from the language server: callers and cross-repo impact of a definition, the providers and consumers of an env var or endpoint, tasks and memories that name a symbol, memories whose symbol refs went stale, and a note when the open branch has no code index.
- The witan view in the activity bar lists, per workspace folder, your own in-progress tasks, the tasks linked to the checked-out branch, the ready tasks for the repo, and its active projects.
- The filter button in the view's title bar picks what those lists cover: this repo (the default), this repo plus work filed under no repo, every repo, or one project in any repo. The choice is kept per workspace and shown next to the view's title.
- Run in Terminal on a task claims it and starts the agent in a terminal; on a project it starts the agent with the project's ready tasks and claims nothing. If the agent cannot start (not on PATH, or the terminal never opens), the extension releases the claim it took. On Windows, `witan.agent` has to name an executable, not an npm `.cmd` shim, because cmd.exe would reparse the prompt.
- Hand Off to Copilot or Claude Code passes the prompt to that agent's chat panel instead, without a claim (the prompt tells the agent to claim first). It appears only when the target's command exists in this VS Code, and only runs when the server says the panel agent would resolve the same witan target and repo as the launch.

## Settings

| setting | default | what it does |
|---|---|---|
| `witan.server.command` | `["witan", "lsp"]` | The argv that starts the language server, one argument per item. To pin every folder to one target, add the option and its value as two items: `["witan", "lsp", "--target", "<name>"]`. Without it, each folder routes to its own target. |
| `witan.agent` | `""` | The agent CLI a launch starts. Empty uses the `agent` in witan's own config. |

Both are machine settings, so a workspace's `.vscode/settings.json` cannot change which programs the extension runs.

## Development

```bash
npm ci
npm run typecheck && npm run lint && npm test
npm run package   # builds dist/ and witan.vsix
npm run smoke     # the .vsix in a pinned VS Code against a real `witan lsp`
```

The smoke test syncs this repo's packages into `.vscode-test/venv` from `uv.lock`, seeds a throwaway store and code graph under `.vscode-test/run`, and needs the omnigraph binary on PATH (`witan setup` installs it) and, on Linux CI, a display (`xvfb-run`).

Changes get a fragment in `changelog.d/` (`just changelog witan-vscode`), and `just bump witan-vscode <part>` releases, as for the Python packages.
