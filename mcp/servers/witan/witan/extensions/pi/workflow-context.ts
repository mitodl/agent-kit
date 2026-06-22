/**
 * witan workflow context — Pi extension
 *
 * Pi equivalent of the Claude Code `workflow-context-inject` (UserPromptSubmit)
 * hook: before each agent turn, injects active WorkflowProjects and ready tasks
 * for the current repo into the system prompt.
 *
 * Delegates to `witan inject-context`, which reads the graph and formats the
 * context block. Requires `witan` on PATH
 * (`uv tool install git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan`).
 *
 * Best-effort: any failure (missing binary, no graph, non-git dir) injects
 * nothing and never disrupts the session.
 *
 * Install: copy or symlink into ~/.pi/agent/extensions/ (via `witan setup --agent pi`
 * or the manual symlink in configs/pi/README.md).
 */

import { spawnSync } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function workflowContextExtension(pi: ExtensionAPI): void {
	pi.on("before_agent_start", async (event: any, ctx: any) => {
		try {
			const r = spawnSync("witan", ["inject-context"], {
				encoding: "utf8",
				timeout: 5000,
				cwd: ctx?.cwd,
			});
			const text = (r.stdout ?? "").trim();
			if (r.status !== 0 || !text) return;
			return { systemPrompt: `${event.systemPrompt ?? ""}\n\n${text}` };
		} catch {
			return;
		}
	});
}
