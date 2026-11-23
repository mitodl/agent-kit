// Chat-panel handoff (ADR 0012, launch mode 3). Both commands are internal to
// their owners and can change in any release, so the client looks for them at
// runtime and the smoke test pins the Copilot one against the VS Code version
// it runs.

/** Opens a new Copilot agent session and submits the prompt (VS Code 1.146). */
export const COPILOT_HANDOFF_COMMAND =
	"workbench.action.chat.openNewSessionSidebar.agent-host-copilotcli";

/** Opens the Claude Code extension's panel with the prompt prefilled. */
export const CLAUDE_HANDOFF_COMMAND = "claude-vscode.editor.open";

export type HandoffTarget = "copilot" | "claude";

export const HANDOFF_COMMANDS: Readonly<Record<HandoffTarget, string>> = {
	copilot: COPILOT_HANDOFF_COMMAND,
	claude: CLAUDE_HANDOFF_COMMAND,
};

/** Which handoff targets have their command registered right now. */
export function availableHandoffs(
	commands: Iterable<string>,
): Record<HandoffTarget, boolean> {
	const registered = new Set(commands);
	return {
		copilot: registered.has(COPILOT_HANDOFF_COMMAND),
		claude: registered.has(CLAUDE_HANDOFF_COMMAND),
	};
}

/** The arguments each target's command takes for `prompt`. */
export function handoffArguments(
	target: HandoffTarget,
	prompt: string,
): unknown[] {
	return target === "copilot" ? [{ prompt }] : [undefined, prompt];
}
