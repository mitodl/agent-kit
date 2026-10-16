/**
 * verify-before-claiming — Pi extension
 *
 * Pi equivalent of the `session_start` hook agent-config.toml writes for
 * Claude Code and Codex (`cat {skill_dir}/SKILL.md`): appends the
 * verify-before-claiming skill's SKILL.md to the system prompt before each
 * agent turn, so the rules are in context without the skill triggering.
 *
 * Reads the installed skill rather than carrying its own copy, so the rules
 * have one source. agent-kit installs Pi's skills to .pi/skills, or to
 * .agents/skills when Codex is also targeted (Pi reads both), at project or
 * global scope. Global wins, so a repo can't replace or blank the rules a
 * global install injects; the project copies (cwd only, not its parents) are
 * the fallback for a project-scope install. None found means no block.
 *
 * Install: `agent-kit apply agent-config.toml` (registered as a plugin hook).
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const SKILL = join("verify-before-claiming", "SKILL.md");

function readRules(cwd: string | undefined): string | undefined {
	const candidates = [
		join(homedir(), ".pi", "agent", "skills", SKILL),
		join(homedir(), ".agents", "skills", SKILL),
		...(cwd
			? [
					join(cwd, ".pi", "skills", SKILL),
					join(cwd, ".agents", "skills", SKILL),
				]
			: []),
	];
	for (const path of candidates) {
		try {
			return readFileSync(path, "utf8").trim();
		} catch {
			/* try the next location */
		}
	}
	return undefined;
}

export default function verifyBeforeClaimingExtension(pi: ExtensionAPI): void {
	pi.on("before_agent_start", async (event: any, ctx: any) => {
		const rules = readRules(ctx?.cwd);
		if (!rules) return;
		return { systemPrompt: `${event.systemPrompt ?? ""}\n\n${rules}` };
	});
}
