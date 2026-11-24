/**
 * The page inside an editor webview (ADR 0011's 2026-11-24 amendment).
 *
 * A webview is served from no witan origin and must hold no credential, so
 * the page does not speak MCP there. It posts each read to the editor, whose
 * extension hands it to the witan language server (`witan.uiRead`), and the
 * server runs it on the folder's own route and refuses anything outside the
 * bound set. Only `mcp.ts` uses the relay; views never see it.
 *
 * VS Code is the one editor with this today, so the bridge is its
 * `acquireVsCodeApi`. Another editor's webview would add its own here.
 */

/** The part of VS Code's webview API the page uses. */
interface WebviewApi {
	postMessage(message: unknown): void;
	getState(): unknown;
	setState(state: unknown): void;
}

/** What `witan.uiTools` answers: each bound read served, with its arguments. */
export interface RelayedTools {
	repo: string | null;
	tools: Record<string, string[]>;
}

/** The editor's relay to the language server. */
export interface HostRelay {
	tools(): Promise<RelayedTools>;
	read(tool: string, args: Record<string, unknown>): Promise<unknown>;
}

/** A message the extension sends back for one request. */
type Response =
	| { witan: "response"; id: string; result: unknown }
	| { witan: "response"; id: string; error: string };

let api: WebviewApi | null | undefined;

/**
 * VS Code's webview API, or `null` in a browser. Memoized because VS Code
 * lets a page acquire it once.
 */
function webviewApi(): WebviewApi | null {
	if (api === undefined) {
		const acquire = (globalThis as { acquireVsCodeApi?: () => WebviewApi })
			.acquireVsCodeApi;
		api = typeof acquire === "function" ? acquire() : null;
	}
	return api;
}

/** Whether the page is running in an editor webview. */
export function inWebview(): boolean {
	return webviewApi() !== null;
}

let relay: HostRelay | null = null;

/** The editor's relay, or `null` when the page is in a browser. */
export function hostRelay(): HostRelay | null {
	const host = webviewApi();
	if (host === null) {
		return null;
	}
	relay ??= createRelay(host, window);
	return relay;
}

function isResponse(data: unknown): data is Response {
	return (
		typeof data === "object" &&
		data !== null &&
		(data as { witan?: unknown }).witan === "response" &&
		typeof (data as { id?: unknown }).id === "string"
	);
}

/**
 * Requests over `host.postMessage`, each answered by one `message` event on
 * `target` carrying its id.
 */
export function createRelay(
	host: Pick<WebviewApi, "postMessage">,
	target: Pick<Window, "addEventListener">,
): HostRelay {
	const pending = new Map<
		string,
		{ resolve(value: unknown): void; reject(error: Error): void }
	>();

	target.addEventListener("message", (event: MessageEvent) => {
		const data: unknown = event.data;
		if (!isResponse(data)) {
			return;
		}
		const waiter = pending.get(data.id);
		if (!waiter) {
			return;
		}
		pending.delete(data.id);
		if ("error" in data) {
			waiter.reject(new Error(data.error));
		} else {
			waiter.resolve(data.result);
		}
	});

	const request = (body: Record<string, unknown>): Promise<unknown> =>
		new Promise((resolve, reject) => {
			// Unique across page loads, not a counter: hiding the panel reloads
			// the page, and an answer to the previous load's request would
			// otherwise resolve whichever new request reused its number.
			const id = crypto.randomUUID();
			pending.set(id, { resolve, reject });
			host.postMessage({ witan: "request", id, ...body });
		});

	return {
		tools: () => request({ kind: "tools" }) as Promise<RelayedTools>,
		read: (tool, args) => request({ kind: "read", tool, args }),
	};
}

/**
 * Route the page inside a webview, where three things differ from a browser.
 *
 * - VS Code's webview host takes every click on a link. A link to a fragment
 *   in the page only scrolls to the element with that id; it is never
 *   followed. The page's tabs, filters and slugs are all fragment links, so
 *   the page follows them itself. A handler that already prevented the
 *   default (a copy button inside a link) keeps its click.
 * - Hiding the panel and showing it again reloads the page. VS Code keeps a
 *   webview's state across that, so the route is saved there on every change
 *   and restored before the app first reads it.
 * - The editor can send the page to a view (`{witan: "navigate", hash}`),
 *   e.g. when it opens the panel on `#board`.
 */
export function routeInWebview(): void {
	const host = webviewApi();
	if (host === null) {
		return;
	}
	const saved = (host.getState() as { hash?: unknown } | undefined)?.hash;
	if (!window.location.hash && typeof saved === "string" && saved) {
		window.location.hash = saved;
	}
	window.addEventListener("hashchange", () => {
		host.setState({ hash: window.location.hash });
	});
	window.addEventListener("message", (event: MessageEvent) => {
		const data: unknown = event.data;
		if (
			typeof data === "object" &&
			data !== null &&
			(data as { witan?: unknown }).witan === "navigate" &&
			typeof (data as { hash?: unknown }).hash === "string"
		) {
			window.location.hash = (data as { hash: string }).hash;
		}
	});
	document.addEventListener("click", (event: MouseEvent) => {
		if (
			event.defaultPrevented ||
			event.button !== 0 ||
			event.metaKey ||
			event.ctrlKey ||
			event.shiftKey ||
			event.altKey
		) {
			return;
		}
		const link = (event.target as Element | null)?.closest?.('a[href^="#"]');
		const href = link?.getAttribute("href");
		if (href) {
			event.preventDefault();
			window.location.hash = href;
		}
	});
}
