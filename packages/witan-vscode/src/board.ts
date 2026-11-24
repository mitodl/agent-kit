// The web UI's views (board, waves, memory, Bridge) in a webview, from the
// bundle the installed witan ships (ADR 0012's amendment on the webview
// relay). The page holds no credential and reaches nothing over the network:
// each read it posts is handed to the language server's `witan.uiRead`, which
// runs it on this panel's folder route and refuses anything outside the web
// UI's bound set (ADR 0011). This module only carries messages.

import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import {
	BOARD_COMMANDS,
	contentSecurityPolicy,
	relayRequest,
	UI_BUNDLE,
	UI_TOOLS,
	webviewHtml,
} from "./page";
import { errorMessage, type ServerCommand } from "./tree";

/** One relayed request, as the smoke test watches them. */
export interface Relayed {
	folder: string;
	kind: "tools" | "read";
	tool?: string;
	/** The server's message when the request failed. */
	error?: string;
}

interface Panel {
	panel: vscode.WebviewPanel;
	/** The page has sent a request, so its listeners are up. */
	ready: boolean;
	/** Where to send the page once it is ready. */
	hash?: string;
}

/** One panel per workspace folder, each reading through that folder. */
export class Boards implements vscode.Disposable {
	private readonly panels = new Map<string, Panel>();
	/** A panel being opened, so a second click waits for it. */
	private readonly opening = new Map<string, Promise<Panel>>();
	private readonly relayed = new vscode.EventEmitter<Relayed>();
	readonly onDidRelay = this.relayed.event;

	constructor(
		private readonly request: ServerCommand,
		private readonly advertised: () => Promise<readonly string[]>,
	) {}

	/**
	 * Open (or reveal) `folder`'s panel, on `hash` when given (e.g. `#board`).
	 * Refuses before opening anything when the server predates the relay.
	 */
	async open(folder: vscode.WorkspaceFolder, hash?: string): Promise<void> {
		const key = folder.uri.toString();
		const open = this.panels.get(key) ?? (await this.opening.get(key));
		if (open) {
			open.panel.reveal();
			if (hash) {
				this.navigate(open, hash);
			}
			return;
		}
		const opening = this.create(folder, key, hash);
		this.opening.set(key, opening);
		try {
			await opening;
		} finally {
			this.opening.delete(key);
		}
	}

	private async create(
		folder: vscode.WorkspaceFolder,
		key: string,
		hash: string | undefined,
	): Promise<Panel> {
		const served = await this.advertised();
		const missing = BOARD_COMMANDS.filter((c) => !served.includes(c));
		if (missing.length > 0) {
			throw new Error(
				`this witan language server does not serve the board (no ${missing.join(", ")}). ` +
					"Upgrade witan: `uv tool upgrade ol-agent-kit`.",
			);
		}
		const bundle = (await this.request(UI_BUNDLE, {})) as { path: string };
		const index = fs.readFileSync(path.join(bundle.path, "index.html"), "utf8");
		const root = vscode.Uri.file(bundle.path);
		const panel = vscode.window.createWebviewPanel(
			"witan.board",
			`witan: ${folder.name}`,
			vscode.ViewColumn.Active,
			{
				enableScripts: true,
				enableCommandUris: false,
				// The page's search boxes are forms it handles itself (a submit
				// listener that prevents the default); without this the sandbox
				// never fires that event. The policy's `form-action 'none'`
				// still stops any form from navigating.
				enableForms: true,
				localResourceRoots: [root],
			},
		);
		const entry: Panel = { panel, ready: false, hash };
		this.panels.set(key, entry);
		panel.onDidDispose(() => this.panels.delete(key));
		panel.webview.onDidReceiveMessage((message: unknown) =>
			this.answer(entry, key, message),
		);
		panel.webview.html = webviewHtml(
			index,
			panel.webview.asWebviewUri(root).toString(),
			contentSecurityPolicy(panel.webview.cspSource),
		);
		return entry;
	}

	/** Send a page that is ready to `hash`; hold it for one that is not. */
	private navigate(entry: Panel, hash: string): void {
		if (entry.ready) {
			void entry.panel.webview.postMessage({ witan: "navigate", hash });
		} else {
			entry.hash = hash;
		}
	}

	private async answer(
		entry: Panel,
		uri: string,
		message: unknown,
	): Promise<void> {
		const request = relayRequest(message, uri);
		if (request === undefined) {
			return;
		}
		if (!entry.ready) {
			// Held until now so it is not posted into a page still loading.
			entry.ready = true;
			if (entry.hash) {
				this.navigate(entry, entry.hash);
				entry.hash = undefined;
			}
		}
		const webview = entry.panel.webview;
		if ("error" in request) {
			void webview.postMessage({ witan: "response", ...request });
			return;
		}
		const relayed: Relayed = {
			folder: uri,
			kind: request.command === UI_TOOLS ? "tools" : "read",
			tool: request.args.tool as string | undefined,
		};
		try {
			const result = await this.request(request.command, request.args);
			void webview.postMessage({ witan: "response", id: request.id, result });
		} catch (err) {
			relayed.error = errorMessage(err);
			void webview.postMessage({
				witan: "response",
				id: request.id,
				error: relayed.error,
			});
		}
		this.relayed.fire(relayed);
	}

	dispose(): void {
		for (const { panel } of this.panels.values()) {
			panel.dispose();
		}
		this.relayed.dispose();
	}
}
