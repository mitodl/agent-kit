/**
 * verify-before-claiming — Pi extension
 *
 * Pi equivalent of the `session_start` hook agent-config.toml writes for
 * Claude Code and Codex (`cat "{skill_dir}/SKILL.md"`): appends the
 * verify-before-claiming skill's SKILL.md to the system prompt before each
 * agent turn, so the rules are in context without the skill triggering.
 *
 * Reads the installed skill rather than carrying its own copy, so the rules
 * have one source. agent-kit installs Pi's skills to .pi/skills, or to the
 * shared .agents/skills when Codex is also targeted (Pi reads both), at
 * project or global scope. A plain apply never removes the copy it stops
 * updating, so after Pi-only -> Pi+Codex (or back) both directories can hold
 * one, and either may be the stale one. The newest SKILL.md of the pair
 * wins: `shutil.copy2` keeps the source's mtime, so the copy from the latest
 * source is the newer file. Global wins over project, so a repo can't
 * replace or blank the rules a global install injects; the project pair
 * (cwd only, not its parents) is the fallback for a project-scope install.
 * None found means no block.
 *
 * Install: `agent-kit apply agent-config.toml` (registered as a plugin hook).
 */

import { readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const SKILL = join("verify-before-claiming", "SKILL.md");

function newest(paths: string[]): string | undefined {
	let best: { path: string; mtimeMs: number } | undefined;
	for (const path of paths) {
		try {
			const { mtimeMs } = statSync(path);
			if (!best || mtimeMs > best.mtimeMs) best = { path, mtimeMs };
		} catch {
			/* not installed here */
		}
	}
	if (!best) return undefined;
	try {
		return readFileSync(best.path, "utf8").trim();
	} catch {
		return undefined;
	}
}

function readRules(cwd: string | undefined): string | undefined {
	const home = homedir();
	return (
		newest([
			join(home, ".pi", "agent", "skills", SKILL),
			join(home, ".agents", "skills", SKILL),
		]) ??
		(cwd
			? newest([
					join(cwd, ".pi", "skills", SKILL),
					join(cwd, ".agents", "skills", SKILL),
				])
			: undefined)
	);
}

export default function verifyBeforeClaimingExtension(pi: ExtensionAPI): void {
	pi.on("before_agent_start", async (event: any, ctx: any) => {
		const rules = readRules(ctx?.cwd);
		if (!rules) return;
		return { systemPrompt: `${event.systemPrompt ?? ""}\n\n${rules}` };
	});
}
