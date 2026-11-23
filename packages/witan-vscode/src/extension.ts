// The VS Code client for the witan language server (ADR 0012). Thin by rule:
// it starts `witan lsp`, binds the server's commands to VS Code UI, and runs
// the launch specs the server returns. Everything witan decides (what is
// ready, what to run, what to claim) comes from the server.

import * as fs from "node:fs";
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
	effectiveEnv,
	findExecutable,
	type LaunchKind,
	type LaunchResult,
	type LaunchSpec,
	terminalEnv,
} from "./launch";
import {
	errorMessage,
	type ProjectNode,
	type ServerCommand,
	TaskNode,
	WorkTreeProvider,
} from "./tree";

const RUN_TASK = "witan.runTask";
const RUN_PROJECT = "witan.runProject";
const RELEASE_TASK = "witan.releaseTask";
const STATUS = "witan.status";

const PROCESS_START_TIMEOUT_MS = 15_000;

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
						`witan: the language server (${[command, ...args].join(" ")}) did not start: ` +
							`${errorMessage(err)}. Install it with \`uv tool install ol-agent-kit\`, ` +
							"or point witan.server.command at it.",
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

	const tree = new WorkTreeProvider(request);

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
			await releaseAfterFailure(result, err);
		} finally {
			tree.refresh();
		}
	};

	const releaseAfterFailure = async (spec: LaunchSpec, err: unknown) => {
		let note = "";
		if (spec.release) {
			try {
				const released = (await request(RELEASE_TASK, { ...spec.release })) as {
					released: boolean;
					reason?: string;
				};
				note = released.released
					? ` Released ${spec.slug}.`
					: ` ${spec.slug} was not released (${released.reason}).`;
			} catch (releaseErr) {
				note = ` Releasing ${spec.slug} failed too: ${errorMessage(releaseErr)}`;
			}
		}
		output.error(`launch of ${spec.slug} failed: ${errorMessage(err)}${note}`);
		void vscode.window.showErrorMessage(
			`witan: could not launch ${spec.slug}: ${errorMessage(err)}.${note}`,
		);
	};

	const handoffs = async () =>
		availableHandoffs(await vscode.commands.getCommands(true));

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
		if (!result.launchable) {
			void vscode.window.showErrorMessage(`witan: ${result.message}`);
			return;
		}
		if (!result.handoff) {
			const choice = await vscode.window.showWarningMessage(
				`witan did not offer ${slug} for a chat-panel handoff. A chat-panel agent ` +
					"starts with VS Code's own environment, which would not resolve the " +
					"same witan target and repo as this launch (more than one folder is " +
					"open, WITAN_TARGET, CLAUDE_PROJECT_DIR or WITAN_REPO is set, or the " +
					"server was started with --target).",
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
		vscode.window.createTreeView("witan.work", {
			treeDataProvider: tree,
			showCollapseAll: true,
		}),
		vscode.commands.registerCommand("witanClient.refresh", () =>
			tree.refresh(),
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

	async function openTerminal(spec: LaunchSpec): Promise<void> {
		const [program = "", ...args] = spec.argv;
		const env = effectiveEnv(terminalBaseEnv(), spec);
		const executable = findExecutable(
			program,
			env,
			process.platform,
			isExecutable,
		);
		if (executable === undefined) {
			throw new LaunchFailed(
				`${program} is not on the PATH a terminal gets. Install it, or set witan.agent`,
			);
		}
		// Merged over the environment any terminal gets; never strictEnv, which
		// would drop everything the person's terminals otherwise have.
		const terminal = vscode.window.createTerminal({
			name: `witan: ${spec.slug}`,
			shellPath: executable,
			shellArgs: args,
			cwd: spec.cwd,
			env: terminalEnv(spec),
		});
		const pid = await Promise.race([
			terminal.processId,
			new Promise<undefined>((resolve) =>
				setTimeout(() => resolve(undefined), PROCESS_START_TIMEOUT_MS),
			),
		]);
		if (pid === undefined) {
			terminal.dispose();
			throw new LaunchFailed("the terminal's process never started");
		}
		terminal.show();
	}
}

export async function deactivate(): Promise<void> {
	await client?.stop();
}

/** The environment a new terminal starts from: this process's, with the
 * person's `terminal.integrated.env.<platform>` applied (`null` removes). */
function terminalBaseEnv(): Record<string, string | undefined> {
	const platform =
		process.platform === "win32"
			? "windows"
			: process.platform === "darwin"
				? "osx"
				: "linux";
	const overrides =
		vscode.workspace
			.getConfiguration("terminal.integrated.env")
			.get<Record<string, string | null>>(platform) ?? {};
	const env: Record<string, string | undefined> = { ...process.env };
	for (const [name, value] of Object.entries(overrides)) {
		env[name] = value ?? undefined;
	}
	return env;
}

function isExecutable(file: string): boolean {
	try {
		fs.accessSync(file, fs.constants.X_OK);
		return fs.statSync(file).isFile();
	} catch {
		return false;
	}
}
