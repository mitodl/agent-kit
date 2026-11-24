// The VS Code client for the witan language server (ADR 0012). Thin by rule:
// it starts `witan lsp`, binds the server's commands to VS Code UI, and runs
// the launch specs the server returns. Everything witan decides (what is
// ready, what to run, what to claim) comes from the server.

import { randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import {
	ExecuteCommandRequest,
	LanguageClient,
	type LanguageClientOptions,
	type ServerOptions,
} from "vscode-languageclient/node";
import {
	availableHandoffs,
	HANDOFF_COMMANDS,
	type HandoffTarget,
	handoffArguments,
} from "./handoff";
import {
	checkVersion,
	type LaunchFile,
	type LaunchKind,
	type LaunchResult,
	type LaunchSpec,
	type LaunchStatus,
	releaseArgs,
} from "./launch";
import {
	type Applied,
	appliedLabel,
	projectChoices,
	REPO_SCOPES,
	requested,
	restoreScope,
	type Scope,
	scopeArgs,
} from "./scope";
import { describeServerCommand } from "./serverCommand";
import {
	errorMessage,
	type ProjectNode,
	type ProjectRow,
	type ServerCommand,
	TaskNode,
	WorkTreeProvider,
} from "./tree";

const RUN_TASK = "witan.runTask";
const RUN_PROJECT = "witan.runProject";
const RELEASE_TASK = "witan.releaseTask";
const LIST_PROJECTS = "witan.listProjects";
const STATUS = "witan.status";
const SCOPE_STATE_KEY = "witan.scope";

const PROCESS_START_TIMEOUT_MS = 15_000;
const STATUS_POLL_MS = 100;

/** What `activate` returns; the smoke test drives the extension through it. */
export interface WitanApi {
	request: ServerCommand;
	tree: WorkTreeProvider;
	launch(
		kind: LaunchKind,
		folder: vscode.WorkspaceFolder,
		slug: string,
	): Promise<void>;
	handoff(
		target: HandoffTarget,
		kind: LaunchKind,
		folder: vscode.WorkspaceFolder,
		slug: string,
	): Promise<void>;
	handoffs(): Promise<Record<HandoffTarget, boolean>>;
}

class LaunchFailed extends Error {}

let client: LanguageClient | undefined;

export async function activate(
	context: vscode.ExtensionContext,
): Promise<WitanApi> {
	const output = vscode.window.createOutputChannel("witan", { log: true });
	context.subscriptions.push(output);
	const settings = () => vscode.workspace.getConfiguration("witan");

	let starting: Promise<LanguageClient> | undefined;

	const start = (): Promise<LanguageClient> => {
		const [command, ...args] = settings().get<string[]>("server.command", [
			"witan",
			"lsp",
		]);
		if (command === undefined) {
			starting = Promise.reject(new Error("witan.server.command is empty."));
			return starting;
		}
		const serverOptions: ServerOptions = { command, args };
		const clientOptions: LanguageClientOptions = {
			// Every file: a stale memory ref or a symbol can be in any language.
			documentSelector: [{ scheme: "file" }],
			outputChannel: output,
		};
		const next = new LanguageClient(
			"witan",
			"witan",
			serverOptions,
			clientOptions,
		);
		client = next;
		starting = next.start().then(
			() => next,
			(err: unknown) => {
				void vscode.window
					.showErrorMessage(
						`witan: the language server did not start: ${errorMessage(err)}. ` +
							`witan.server.command is ${describeServerCommand([command, ...args])} ` +
							"If the server ran and exited, the witan output has its own error. Install the server with " +
							"`uv tool install ol-agent-kit`, or correct witan.server.command.",
						"Open Settings",
					)
					.then((choice) => {
						if (choice) {
							void vscode.commands.executeCommand(
								"workbench.action.openSettings",
								"witan.server.command",
							);
						}
					});
				throw err;
			},
		);
		return starting;
	};

	const running = (): Promise<LanguageClient> => starting ?? start();

	const request: ServerCommand = async (command, args) => {
		const c = await running();
		return c.sendRequest(ExecuteCommandRequest.type, {
			command,
			arguments: [args],
		});
	};

	const tree = new WorkTreeProvider(
		request,
		restoreScope(context.workspaceState.get(SCOPE_STATE_KEY)),
	);
	const treeView = vscode.window.createTreeView("witan.work", {
		treeDataProvider: tree,
		showCollapseAll: true,
	});
	const describe = (applied: Applied) => {
		treeView.description = appliedLabel(applied, tree.scope);
	};
	describe(requested(tree.scope));
	tree.onDidApplyScope(describe);

	const pickScope = async (): Promise<void> => {
		const current = scopeArgs(tree.scope);
		const mark = (on: boolean) => (on ? "current" : undefined);
		const pick = await vscode.window.showQuickPick(
			[
				...REPO_SCOPES.map((s) => ({
					label: s.label,
					description: mark(current.scope === s.scope),
					choose: async (): Promise<Scope> => ({ scope: s.scope }),
				})),
				{
					label: "A project…",
					description: mark("project" in current),
					choose: pickProject,
				},
			],
			{ title: "witan: show work from" },
		);
		const scope = await pick?.choose();
		if (scope === undefined) {
			return;
		}
		await context.workspaceState.update(SCOPE_STATE_KEY, scope);
		tree.setScope(scope);
		describe(requested(scope));
	};

	const pickProject = async (): Promise<Scope | undefined> => {
		const folders = vscode.workspace.workspaceFolders ?? [];
		if (folders.length === 0) {
			throw new Error("open a folder to pick one of its projects.");
		}
		// Every folder: in a multi-root workspace each can route to its own graph.
		const listed = await Promise.allSettled(
			folders.map(async (f) => {
				const folder = f.uri.toString();
				const { projects } = (await request(LIST_PROJECTS, {
					uri: folder,
					scope: "all",
				})) as { projects: ProjectRow[] };
				return { folder, projects };
			}),
		);
		const lists = listed.flatMap((r, i) => {
			if (r.status === "fulfilled") {
				return [r.value];
			}
			output.warn(
				`listing ${folders[i]?.name}'s projects failed: ${errorMessage(r.reason)}`,
			);
			return [];
		});
		const [first] = listed;
		if (lists.length === 0 && first?.status === "rejected") {
			throw first.reason;
		}
		const names = new Map(folders.map((f) => [f.uri.toString(), f.name]));
		const pick = await vscode.window.showQuickPick(
			projectChoices(lists).map((p) => ({
				label: p.title,
				description: [p.phase, p.slug].filter(Boolean).join(" · "),
				detail: folders.length > 1 ? names.get(p.folder) : undefined,
				choice: p,
			})),
			{ title: "witan: show one project's work", matchOnDescription: true },
		);
		return (
			pick && {
				project: pick.choice.slug,
				title: pick.choice.title,
				folder: pick.choice.folder,
			}
		);
	};

	const restart = async () => {
		const previous = client;
		starting = undefined;
		client = undefined;
		await previous?.stop().catch(() => undefined);
		await running().catch(() => undefined);
		tree.refresh();
	};

	const launch = async (
		kind: LaunchKind,
		folder: vscode.WorkspaceFolder,
		slug: string,
	): Promise<void> => {
		const agent = settings().get<string>("agent", "");
		const result = (await request(kind === "task" ? RUN_TASK : RUN_PROJECT, {
			uri: folder.uri.toString(),
			slug,
			...(agent ? { agent } : {}),
		})) as LaunchResult;
		// A spec of a version this client does not know is refused whole. If it
		// claimed, the claim lapses with its lease: the release arguments are
		// part of what the unknown version may have changed.
		checkVersion(result);
		if (!result.launchable) {
			void vscode.window.showErrorMessage(`witan: ${result.message}`);
			return;
		}
		try {
			await openTerminal(result);
		} catch (err) {
			await releaseAfterFailure(result, folder, err);
		} finally {
			tree.refresh();
		}
	};

	/** Release a launch's claim, and say how that went in one sentence. */
	const release = async (
		result: LaunchResult,
		folder: vscode.WorkspaceFolder,
	): Promise<string> => {
		const args = releaseArgs(result, folder.uri.toString());
		if (args === undefined) {
			return "";
		}
		try {
			const released = (await request(RELEASE_TASK, { ...args })) as {
				released: boolean;
				reason?: string;
			};
			return released.released
				? ` Released ${result.slug}.`
				: ` ${result.slug} was not released (${released.reason}).`;
		} catch (releaseErr) {
			return ` Releasing ${result.slug} failed too: ${errorMessage(releaseErr)}`;
		}
	};

	const releaseAfterFailure = async (
		spec: LaunchSpec,
		folder: vscode.WorkspaceFolder,
		err: unknown,
	) => {
		const note = await release(spec, folder);
		output.error(`launch of ${spec.slug} failed: ${errorMessage(err)}${note}`);
		void vscode.window.showErrorMessage(
			`witan: could not launch ${spec.slug}: ${errorMessage(err)}.${note}`,
		);
	};

	// A command an extension contributes is listed before the extension has
	// activated and registered it; running it activates the extension.
	const handoffs = async () =>
		availableHandoffs([
			...(await vscode.commands.getCommands(true)),
			...vscode.extensions.all.flatMap((e) =>
				// The manifest schema takes one command object or an array of them.
				[
					(e.packageJSON?.contributes?.commands ?? []) as
						| { command: string }
						| Array<{ command: string }>,
				]
					.flat()
					.map((c) => c.command),
			),
		]);

	const handoff = async (
		target: HandoffTarget,
		kind: LaunchKind,
		folder: vscode.WorkspaceFolder,
		slug: string,
	): Promise<void> => {
		// Never a claim: a prefilled prompt nobody submits would hold the lease
		// for an hour with no failed launch to release it on.
		const result = (await request(kind === "task" ? RUN_TASK : RUN_PROJECT, {
			uri: folder.uri.toString(),
			slug,
			...(kind === "task" ? { claim: false } : {}),
		})) as LaunchResult;
		checkVersion(result);
		if (result.claim) {
			// A witan-council before `claim: false` existed claims anyway.
			const note = await release(result, folder);
			tree.refresh();
			throw new Error(
				`the witan language server claimed ${slug} for a chat-panel handoff, ` +
					`which a handoff never does, so this witan predates handoffs. ` +
					`Upgrade witan to hand off.${note}`,
			);
		}
		if (!result.launchable) {
			void vscode.window.showErrorMessage(`witan: ${result.message}`);
			return;
		}
		if (!result.handoff) {
			const choice = await vscode.window.showWarningMessage(
				`witan did not offer ${slug} for a chat-panel handoff. Either this ` +
					"witan does not offer handoffs yet, or a chat-panel agent, which " +
					"starts with VS Code's own environment, would not resolve the same " +
					"witan target and repo as this launch (more than one folder is open, " +
					"WITAN_TARGET, CLAUDE_PROJECT_DIR or WITAN_REPO is set, or the server " +
					"was started with --target).",
				"Run in Terminal",
			);
			if (choice) {
				await launch(kind, folder, slug);
			}
			return;
		}
		await vscode.commands.executeCommand(
			HANDOFF_COMMANDS[target],
			...handoffArguments(target, result.prompt),
		);
	};

	const updateHandoffContext = async () => {
		const available = await handoffs();
		for (const target of Object.keys(available) as HandoffTarget[]) {
			await vscode.commands.executeCommand(
				"setContext",
				`witan.handoff.${target}`,
				available[target],
			);
		}
	};

	const nodeArgs = (
		node: TaskNode | ProjectNode,
	): [LaunchKind, vscode.WorkspaceFolder, string] =>
		node instanceof TaskNode
			? ["task", node.folder, node.task.slug]
			: ["project", node.folder, node.project.slug];

	const reportFailure =
		<T extends unknown[]>(fn: (...args: T) => Promise<void>) =>
		async (...args: T) => {
			try {
				await fn(...args);
			} catch (err) {
				output.error(errorMessage(err));
				void vscode.window.showErrorMessage(`witan: ${errorMessage(err)}`);
			}
		};

	context.subscriptions.push(
		treeView,
		vscode.commands.registerCommand("witanClient.refresh", () =>
			tree.refresh(),
		),
		vscode.commands.registerCommand(
			"witanClient.pickScope",
			reportFailure(pickScope),
		),
		vscode.commands.registerCommand("witanClient.restartServer", restart),
		vscode.commands.registerCommand(
			"witanClient.showStatus",
			reportFailure(async () => {
				output.info(JSON.stringify(await request(STATUS, {}), null, 2));
				output.show();
			}),
		),
		vscode.commands.registerCommand(
			"witanClient.runInTerminal",
			reportFailure((node: TaskNode | ProjectNode) =>
				launch(...nodeArgs(node)),
			),
		),
		vscode.commands.registerCommand(
			"witanClient.handoffToCopilot",
			reportFailure((node: TaskNode | ProjectNode) =>
				handoff("copilot", ...nodeArgs(node)),
			),
		),
		vscode.commands.registerCommand(
			"witanClient.handoffToClaude",
			reportFailure((node: TaskNode | ProjectNode) =>
				handoff("claude", ...nodeArgs(node)),
			),
		),
		vscode.workspace.onDidChangeConfiguration((event) => {
			if (event.affectsConfiguration("witan.server.command")) {
				void restart();
			}
		}),
		// The server resolves folders when they are added; the tree asks per folder.
		vscode.workspace.onDidChangeWorkspaceFolders(() => tree.refresh()),
		vscode.extensions.onDidChange(() => void updateHandoffContext()),
	);

	await updateHandoffContext();
	void running().catch(() => undefined);

	return { request, tree, launch, handoff, handoffs };

	/**
	 * Run `spec` in a new terminal through the launcher (src/launcher.ts), and
	 * resolve once the agent has started. The spec goes in a file, not in the
	 * terminal's args, env or cwd, which VS Code would resolve `${...}` in.
	 */
	async function openTerminal(spec: LaunchSpec): Promise<void> {
		const dir = path.join(context.globalStorageUri.fsPath, "launches");
		fs.mkdirSync(dir, { recursive: true });
		const id = randomUUID();
		const file = path.join(dir, `${id}.json`);
		const status = path.join(dir, `${id}.status.json`);
		const launchFile: LaunchFile = {
			argv: spec.argv,
			cwd: spec.cwd,
			env: spec.env,
			unset_env: spec.unset_env,
			status,
		};
		// The prompt is in it; the launcher deletes it as it reads it.
		fs.writeFileSync(file, JSON.stringify(launchFile), { mode: 0o600 });
		const terminal = vscode.window.createTerminal({
			name: `witan: ${spec.slug}`,
			// Reviving it after a restart would rerun a launch file already gone.
			isTransient: true,
			// The editor's own runtime as plain node: the extension host's binary,
			// which in desktop VS Code is Electron.
			shellPath: process.execPath,
			shellArgs: [context.asAbsolutePath("dist/launcher.cjs"), file],
			env: { ELECTRON_RUN_AS_NODE: "1" },
		});
		let result: LaunchStatus | undefined;
		try {
			result = await readStatus(status);
		} finally {
			fs.rmSync(file, { force: true });
			fs.rmSync(status, { force: true });
		}
		if (result === undefined) {
			terminal.dispose();
			throw new LaunchFailed("the terminal's process never started");
		}
		terminal.show();
		if ("error" in result) {
			throw new LaunchFailed(result.error);
		}
	}
}

/** The launcher's report, or `undefined` if none came in time. */
async function readStatus(file: string): Promise<LaunchStatus | undefined> {
	const deadline = Date.now() + PROCESS_START_TIMEOUT_MS;
	while (Date.now() < deadline) {
		if (fs.existsSync(file)) {
			return JSON.parse(fs.readFileSync(file, "utf8")) as LaunchStatus;
		}
		await new Promise((resolve) => setTimeout(resolve, STATUS_POLL_MS));
	}
	return undefined;
}

export async function deactivate(): Promise<void> {
	await client?.stop();
}
