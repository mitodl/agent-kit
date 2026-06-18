/**
 * witan workflow context — Pi extension
 *
 * Pi equivalent of the Claude Code `workflow-context-inject` (UserPromptSubmit)
 * hook: before each agent turn, look up the current repo's active
 * WorkflowProjects and ready tasks and append them to the system prompt, so a
 * Pi session can discover what work it should link to.
 *
 * Best-effort: any failure (no repo, omnigraph unavailable, no data) injects
 * nothing. Requires the `omnigraph` binary on PATH and the witan
 * graph initialised.
 *
 * Install: symlink into ~/.pi/agent/extensions/ (see configs/pi/README.md).
 */

import { spawnSync } from "node:child_process";
import { realpathSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// read.gq lives in the witan package, three levels up from this
// extension (configs/pi/extensions → repo root). Resolve through the symlink.
const HERE = dirname(realpathSync(fileURLToPath(import.meta.url)));
const READ_GQ = resolve(HERE, "../../../mcp/servers/witan/queries/read.gq");

const GRAPH_URI =
	process.env.WITAN_MEMORY_URI ??
	`${process.env.HOME}/.local/share/witan/graph.omni`;

const PRIORITY: Record<string, number> = { p0: 0, p1: 1, p2: 2, p3: 3 };

/** Canonical HTTPS project URI — must match witan/repo.py::_normalise. */
function normalizeRemote(url: string): string {
	url = url
		.trim()
		.replace(/\.git$/, "")
		.replace(/\/+$/, "");
	let m = url.match(/^(?:ssh:\/\/)?[^@]+@([^:/]+)[:/](.+)$/);
	if (m) return `https://${m[1]}/${m[2]}`;
	m = url.match(/^https?:\/\/(?:[^@/]+@)?([^/]+)\/(.+)$/);
	if (m) return `https://${m[1]}/${m[2]}`;
	return url;
}

function repoKey(cwd: string): string | null {
	const r = spawnSync("git", ["-C", cwd, "remote", "get-url", "origin"], {
		encoding: "utf8",
	});
	if (r.status !== 0) return null;
	const url = (r.stdout || "").trim();
	return url ? normalizeRemote(url) : null;
}

/** Strip alias prefixes: "p.slug" -> "slug" (mirrors graph.py). */
function strip(row: Record<string, unknown>): Record<string, any> {
	const out: Record<string, any> = {};
	for (const [k, v] of Object.entries(row)) {
		const i = k.indexOf(".");
		out[i === -1 ? k : k.slice(i + 1)] = v;
	}
	return out;
}

function query(
	name: string,
	params: Record<string, unknown>,
): Record<string, any>[] {
	const r = spawnSync(
		"omnigraph",
		[
			"query",
			"--store",
			GRAPH_URI,
			"--query",
			READ_GQ,
			name,
			"--params",
			JSON.stringify(params),
			"--format",
			"json",
		],
		{ encoding: "utf8", timeout: 5000 },
	);
	if (r.status !== 0 || !r.stdout) return [];
	try {
		const data = JSON.parse(r.stdout);
		const rows = Array.isArray(data) ? data : (data.rows ?? []);
		return rows.map(strip);
	} catch {
		return [];
	}
}

function buildBlock(
	projects: Record<string, any>[],
	ready: Record<string, any>[],
): string {
	const lines: string[] = [];
	if (projects.length) {
		lines.push("## Active Workflow Projects", "");
		lines.push(
			`This repository has ${projects.length} active tracked project(s):`,
			"",
		);
		for (const p of projects.slice(0, 3)) {
			lines.push(`- **${p.title}** (slug: \`${p.slug}\`)`);
			lines.push(`  Phase: ${p.phase}`);
			if (p.github_issue) lines.push(`  Issue: ${p.github_issue}`);
		}
		lines.push(
			"",
			"Call `workflow_session_start` with the matching slug if this session contributes to one of them.",
			"",
		);
	}
	if (ready.length) {
		lines.push("## Ready Tasks", "");
		lines.push(
			`${ready.length} task(s) are ready to work (open, no open blockers):`,
			"",
		);
		for (const t of ready.slice(0, 5)) {
			const ext = t.external_uri ? ` · ${t.external_uri}` : "";
			lines.push(
				`- \`[${t.priority ?? "p2"}]\` **${t.title}** (slug: \`${t.slug}\`)${ext}`,
			);
		}
		lines.push(
			"",
			"Use `task_update`/`task_close` (or the `/task` skill) to claim and progress them.",
		);
	}
	return lines.join("\n");
}

export default function workflowContextExtension(pi: ExtensionAPI): void {
	pi.on("before_agent_start", async (event: any, ctx) => {
		try {
			const repo = repoKey(ctx.cwd);
			if (!repo) return;

			// Projects use a repo SET (`repos`) that can't be match-filtered in
			// the query; fetch all active and keep those whose set contains repo.
			const projects = query("list_projects_by_status", {
				status: "active",
			}).filter((p) => ((p.repos as string[] | null) ?? []).includes(repo));
			const tasks = query("list_tasks_by_repo", { repo });

			const statusBySlug = new Map(tasks.map((t) => [t.slug, t.status]));
			// Matches the server's task_ready: status open OR blocked, all blockers closed.
			const ready = tasks
				.filter(
					(t) =>
						(t.status === "open" || t.status === "blocked") &&
						(t.blocked_by ?? []).every(
							(b: string) => (statusBySlug.get(b) ?? "closed") === "closed",
						),
				)
				.sort(
					(a, b) => (PRIORITY[a.priority] ?? 9) - (PRIORITY[b.priority] ?? 9),
				);

			if (!projects.length && !ready.length) return;

			const block = buildBlock(projects, ready);
			return { systemPrompt: `${event.systemPrompt ?? ""}\n\n${block}` };
		} catch {
			return;
		}
	});
}
