// Runs inside the VS Code that test/smoke/run.mjs starts, with the packaged
// extension loaded and a real `witan lsp` behind it. Bundled to
// out/smoke/suite.cjs by esbuild.mjs.

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import type { WitanApi } from "../../src/extension";
// Types only: the suite is its own bundle, so the extension's node classes
// are not the ones `instanceof` would see here.
import type { SectionNode, TaskNode, WorkNode } from "../../src/tree";

interface Smoke {
	repo: string;
	record: string;
	launchTask: string;
	failTask: string;
	symbolTask: string;
	project: string;
	symbol: string;
}

const TIMEOUT_MS = 60_000;

async function until<T>(
	what: string,
	probe: () => Promise<T | undefined> | T | undefined,
): Promise<T> {
	const deadline = Date.now() + TIMEOUT_MS;
	for (;;) {
		const value = await probe();
		if (value !== undefined) {
			return value;
		}
		if (Date.now() > deadline) {
			throw new Error(`timed out waiting for ${what}`);
		}
		await new Promise((resolve) => setTimeout(resolve, 250));
	}
}

function agentRuns(smoke: Smoke): Array<{
	argv: string[];
	cwd: string;
	env: Record<string, string | null>;
}> {
	return fs
		.readdirSync(smoke.record)
		.map((f) =>
			JSON.parse(fs.readFileSync(path.join(smoke.record, f), "utf8")),
		);
}

async function sectionSlugs(api: WitanApi, label: string): Promise<string[]> {
	api.tree.refresh();
	const sections = await api.tree.getChildren();
	const section = sections.find(
		(s: WorkNode): s is SectionNode => "rows" in s && s.label === label,
	);
	assert.ok(section, `no ${label} section in ${sections.length} root nodes`);
	return (await api.tree.getChildren(section))
		.filter((n): n is TaskNode => "task" in n)
		.map((n) => n.task.slug);
}

const checks: Array<[string, (api: WitanApi, smoke: Smoke) => Promise<void>]> =
	[
		[
			"the server starts with both plugins",
			async (api) => {
				const status = (await api.request("witan.status", {})) as {
					plugins: Array<{ name: string }>;
				};
				assert.deepEqual(status.plugins.map((p) => p.name).sort(), [
					"code",
					"council",
				]);
			},
		],
		[
			"the tree lists ready work for the open repo",
			async (api, smoke) => {
				const ready = await sectionSlugs(api, "Ready");
				for (const slug of [
					smoke.launchTask,
					smoke.failTask,
					smoke.symbolTask,
				]) {
					assert.ok(ready.includes(slug), `${slug} not in Ready: ${ready}`);
				}
			},
		],
		[
			"a task launches into a terminal with the spec's env and a claim",
			async (api, smoke) => {
				const folder = vscode.workspace.workspaceFolders?.[0];
				assert.ok(folder);
				await api.launch("task", folder, smoke.launchTask);
				const run = await until("the agent to run", () =>
					agentRuns(smoke).find((r) =>
						r.argv.at(-1)?.includes(smoke.launchTask),
					),
				);
				assert.equal(fs.realpathSync(run.cwd), fs.realpathSync(smoke.repo));
				// unset_env took the editor's agent-session variables back out, the
				// local store left WITAN_TARGET unset, and nothing else was dropped.
				assert.deepEqual(run.env, {
					CLAUDE_SESSION_ID: null,
					CLAUDE_PROJECT_DIR: null,
					WITAN_REPO: null,
					WITAN_TARGET: null,
					SMOKE_KEPT: "kept",
				});
				const mine = await sectionSlugs(api, "In progress (mine)");
				assert.ok(mine.includes(smoke.launchTask), `not in progress: ${mine}`);
			},
		],
		[
			"a launch whose agent is missing releases the claim it took",
			async (api, smoke) => {
				const folder = vscode.workspace.workspaceFolders?.[0];
				assert.ok(folder);
				const settings = vscode.workspace.getConfiguration("witan");
				const agent = settings.inspect<string>("agent")?.globalValue;
				await settings.update(
					"agent",
					"no-such-agent",
					vscode.ConfigurationTarget.Global,
				);
				try {
					await api.launch("task", folder, smoke.failTask);
				} finally {
					await settings.update(
						"agent",
						agent,
						vscode.ConfigurationTarget.Global,
					);
				}
				const ready = await sectionSlugs(api, "Ready");
				assert.ok(ready.includes(smoke.failTask), `still claimed: ${ready}`);
				const mine = await sectionSlugs(api, "In progress (mine)");
				assert.ok(!mine.includes(smoke.failTask));
			},
		],
		[
			"a project launches into a terminal and claims nothing",
			async (api, smoke) => {
				const folder = vscode.workspace.workspaceFolders?.[0];
				assert.ok(folder);
				const before = agentRuns(smoke).length;
				await api.launch("project", folder, smoke.project);
				const run = await until("the project agent to run", () =>
					agentRuns(smoke).length > before
						? agentRuns(smoke).find((r) =>
								r.argv.at(-1)?.includes(smoke.project),
							)
						: undefined,
				);
				assert.equal(run.env.SMOKE_KEPT, "kept");
			},
		],
		[
			`the Copilot handoff command exists in VS Code ${vscode.version}`,
			async (api) => {
				assert.equal((await api.handoffs()).copilot, true);
			},
		],
		[
			"hover shows the task naming the symbol",
			async (_api, smoke) => {
				const doc = await vscode.workspace.openTextDocument(
					path.join(smoke.repo, "app", "svc.py"),
				);
				await vscode.window.showTextDocument(doc);
				const text = await until("a witan hover", async () => {
					const hovers = await vscode.commands.executeCommand<vscode.Hover[]>(
						"vscode.executeHoverProvider",
						doc.uri,
						new vscode.Position(0, 7),
					);
					const value = hovers
						.flatMap((h) => h.contents)
						.map((c) => (typeof c === "string" ? c : c.value))
						.join("\n");
					return value.includes("witan") ? value : undefined;
				});
				assert.ok(
					text.includes(smoke.symbolTask.replaceAll("-", "\\-")),
					`hover has no ${smoke.symbolTask}: ${text}`,
				);
			},
		],
		[
			"CodeLens, definition and references come from the code graph",
			async (_api, smoke) => {
				const svc = vscode.Uri.file(path.join(smoke.repo, "app", "svc.py"));
				const lenses = await until("CodeLens", async () => {
					const found = await vscode.commands.executeCommand<vscode.CodeLens[]>(
						"vscode.executeCodeLensProvider",
						svc,
						10,
					);
					return found.some((l) => l.command?.title) ? found : undefined;
				});
				assert.ok(lenses.length > 0);
				// `helper()` on line 2 is defined on line 5.
				const definitions = await vscode.commands.executeCommand<
					Array<vscode.Location | vscode.LocationLink>
				>("vscode.executeDefinitionProvider", svc, new vscode.Position(2, 16));
				assert.ok(
					definitions.some(
						(d) => ("range" in d ? d.range : d.targetRange).start.line === 5,
					),
					`no definition on line 5: ${JSON.stringify(definitions)}`,
				);
				const references = await vscode.commands.executeCommand<
					vscode.Location[]
				>("vscode.executeReferenceProvider", svc, new vscode.Position(0, 7));
				assert.ok(
					references.some((r) => r.uri.path.endsWith("app/main.py")),
					`no reference from main.py: ${JSON.stringify(references)}`,
				);
			},
		],
	];

export async function run(): Promise<void> {
	const smoke = JSON.parse(process.env.WITAN_SMOKE ?? "") as Smoke;
	const extension = vscode.extensions.getExtension<WitanApi>("mitol.witan");
	assert.ok(extension, "mitol.witan is not installed");
	const api = await extension.activate();
	const failures: string[] = [];
	for (const [name, check] of checks) {
		try {
			await check(api, smoke);
			console.log(`ok    ${name}`);
		} catch (err) {
			failures.push(name);
			console.log(
				`FAIL  ${name}\n      ${err instanceof Error ? err.stack : err}`,
			);
		}
	}
	if (failures.length > 0) {
		throw new Error(
			`${failures.length} smoke check(s) failed: ${failures.join("; ")}`,
		);
	}
}
