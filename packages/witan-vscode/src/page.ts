// The webview page's side of the board, without VS Code: the policy and
// HTML the panel serves the bundle with, and how a page message becomes a
// server command. Kept free of `vscode` so the unit tests run it in node.

export const UI_BUNDLE = "witan.uiBundle";
export const UI_TOOLS = "witan.uiTools";
export const UI_READ = "witan.uiRead";

/** What the panel needs the server to advertise. */
export const BOARD_COMMANDS = [UI_BUNDLE, UI_TOOLS, UI_READ] as const;

/** A page message turned into the server command it asks for. */
export type RelayRequest =
	| { id: string; command: string; args: Record<string, unknown> }
	| { id: string; error: string };

/**
 * The webview's policy: the bundle's own scripts, styles, fonts and images,
 * and nothing else. No inline script or style, no network, no frames. The
 * same as the `/ui/` routes serve the page with (`default-src 'self'`), with
 * the webview's resource source (`webview.cspSource`) in place of `'self'`:
 * a webview document's own origin serves none of the bundle's files.
 */
export function contentSecurityPolicy(source: string): string {
	return [
		"default-src 'none'",
		`script-src ${source}`,
		`style-src ${source}`,
		`font-src ${source}`,
		`img-src ${source}`,
		"base-uri 'none'",
		"form-action 'none'",
	].join("; ");
}

/**
 * The bundle's `index.html` for a webview: its script and stylesheet URLs,
 * which name the `/ui/` mount, pointed at `assetBase`, and the policy first
 * in its head. Everything else in the bundle names its files relative to
 * itself, so these two are the only URLs to move.
 */
export function webviewHtml(
	index: string,
	assetBase: string,
	csp: string,
): string {
	let moved = 0;
	const html = index.replace(/(\s(?:src|href)=")\/ui\//g, (_, attr) => {
		moved += 1;
		return `${attr}${assetBase}/`;
	});
	if (moved === 0 || !html.includes("<head>")) {
		throw new Error(
			"witan's web UI bundle is not the shape this extension expects (no " +
				"<head>, or no /ui/ asset URL in index.html). Upgrade the extension " +
				"or witan so they match.",
		);
	}
	const policy = csp.replaceAll('"', "&quot;");
	return html.replace(
		"<head>",
		`<head>\n    <meta http-equiv="Content-Security-Policy" content="${policy}" />`,
	);
}

/**
 * The server command a page message asks for, with `uri` set to the panel's
 * folder. `undefined` for a message that is not a request at all. The page
 * picks neither the command nor the folder: a read can only be `witan.uiRead`
 * for this panel's folder, and the server decides whether the tool is one.
 */
export function relayRequest(
	message: unknown,
	uri: string,
): RelayRequest | undefined {
	if (typeof message !== "object" || message === null) {
		return undefined;
	}
	const { witan, id, kind, tool, args } = message as Record<string, unknown>;
	if (witan !== "request" || typeof id !== "string") {
		return undefined;
	}
	if (kind === "tools") {
		return { id, command: UI_TOOLS, args: { uri } };
	}
	if (
		kind === "read" &&
		typeof tool === "string" &&
		(args === undefined ||
			(typeof args === "object" && args !== null && !Array.isArray(args)))
	) {
		return {
			id,
			command: UI_READ,
			args: { uri, tool, arguments: args ?? {} },
		};
	}
	return {
		id,
		error: "witan: the page sent a request this extension does not relay.",
	};
}
