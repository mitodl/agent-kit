/**
 * witan-code — Pi extension
 *
 * Pi equivalent of the two Claude Code code-graph hooks:
 *  - session_start : seed/refresh the whole repo's Layer-2 code graph in the
 *                    background (first session builds it, later sessions re-hash
 *                    and skip unchanged files).
 *  - edit / write  : incrementally re-index the edited file after the tool runs.
 *
 * Best-effort and non-blocking: a missing CLI, non-git dir, or parse failure
 * never disrupts the session. Requires `witan-code` on PATH
 * (`uv tool install --editable mcp/servers/witan-code`); otherwise it
 * silently no-ops.
 *
 * Install: symlink into ~/.pi/agent/extensions/ (see configs/pi/README.md).
 */

import { execSync, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const SRC_EXT = /\.(py|pyi|ts|tsx|js|jsx|mjs|cjs)$/;
const EDIT_TOOLS = new Set(["edit", "write"]);

function inGitRepo(cwd: string): boolean {
	try {
		execSync("git rev-parse --is-inside-work-tree", {
			cwd,
			stdio: ["ignore", "ignore", "ignore"],
		});
		return true;
	} catch {
		return false;
	}
}

/** Run the indexer detached in the background; ignore all failures. */
function indexInBackground(target: string): void {
	try {
		const child = spawn("witan-code", ["index", target], {
			detached: true,
			stdio: "ignore",
		});
		child.on("error", () => {}); // CLI not installed, etc.
		child.unref();
	} catch {
		/* ignore */
	}
}

function editedPath(event: any, cwd: string): string | null {
	const input = event?.input ?? {};
	const raw = input.path ?? input.file_path ?? input.filename;
	if (typeof raw !== "string" || raw.length === 0) return null;
	const abs = isAbsolute(raw) ? raw : resolve(cwd, raw);
	return SRC_EXT.test(abs) ? abs : null;
}

export default function codegraphExtension(pi: ExtensionAPI): void {
	// Paths captured at tool_call, consumed at tool_result (FIFO) — covers Pi
	// builds where tool_result carries no input.
	const pending: string[] = [];

	// Seed / refresh the whole repo once per session (incremental).
	pi.on("session_start", async (_event, ctx) => {
		if (inGitRepo(ctx.cwd)) indexInBackground(ctx.cwd);
	});

	// Push one entry per edit tool_call (path or "") so the FIFO stays paired
	// 1:1 with tool_result, which always shifts exactly one.
	pi.on("tool_call", (event: any, ctx) => {
		if (!EDIT_TOOLS.has(event?.toolName)) return;
		pending.push(editedPath(event, ctx.cwd) ?? "");
	});

	// After a successful edit/write, re-index just that file.
	pi.on("tool_result", (event: any, ctx) => {
		if (!EDIT_TOOLS.has(event?.toolName)) return;
		const queued = pending.shift() ?? "";
		if (event?.isError) return;
		const p = editedPath(event, ctx.cwd) ?? queued;
		if (p && existsSync(p)) indexInBackground(p);
	});
}
