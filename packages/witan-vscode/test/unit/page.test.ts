import assert from "node:assert/strict";
import { test } from "node:test";
import {
	contentSecurityPolicy,
	relayRequest,
	UI_READ,
	UI_TOOLS,
	webviewHtml,
} from "../../src/page.ts";

const URI = "file:///home/me/code/repo";

// The bundle's index.html as `vite build` writes it (base "/ui/").
const INDEX = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Witan</title>
    <script type="module" crossorigin src="/ui/assets/index-a.js"></script>
    <link rel="stylesheet" crossorigin href="/ui/assets/index-b.css">
  </head>
  <body>
    <div id="app"></div>
  </body>
</html>
`;

test("the policy allows the bundle's own files and nothing inline or remote", () => {
	const csp = contentSecurityPolicy("https://*.vscode-cdn.net");
	assert.match(csp, /^default-src 'none'/);
	assert.match(csp, /script-src https:\/\/\*\.vscode-cdn\.net(;|$)/);
	assert.doesNotMatch(csp, /unsafe-inline|unsafe-eval|connect-src|\*\s|data:/);
});

test("the html loads its two assets from the webview and carries the policy", () => {
	const html = webviewHtml(
		INDEX,
		"https://file+.vscode-resource.vscode-cdn.net/site/witan/ui_dist",
		"default-src 'none'",
	);
	assert.match(
		html,
		/src="https:\/\/file\+\.vscode-resource\.vscode-cdn\.net\/site\/witan\/ui_dist\/assets\/index-a\.js"/,
	);
	assert.match(
		html,
		/href="https:\/\/file\+\.vscode-resource\.vscode-cdn\.net\/site\/witan\/ui_dist\/assets\/index-b\.css"/,
	);
	assert.doesNotMatch(html, /"\/ui\//);
	// First in the head, so it governs everything after it.
	assert.match(
		html,
		/<head>\s*<meta http-equiv="Content-Security-Policy" content="default-src 'none'" \/>/,
	);
});

test("a bundle of another shape is refused rather than served broken", () => {
	assert.throws(
		() => webviewHtml("<html><head></head></html>", "https://x", "c"),
		/not the shape this extension expects/,
	);
});

test("the page asks for tools or one read, and the folder is the panel's", () => {
	assert.deepEqual(
		relayRequest({ witan: "request", id: 1, kind: "tools" }, URI),
		{
			id: 1,
			command: UI_TOOLS,
			args: { uri: URI },
		},
	);
	assert.deepEqual(
		relayRequest(
			{
				witan: "request",
				id: 2,
				kind: "read",
				tool: "task_get",
				args: { slug: "tk-a" },
				// A page cannot pick another folder or command.
				uri: "file:///elsewhere",
				command: "witan.runTask",
			},
			URI,
		),
		{
			id: 2,
			command: UI_READ,
			args: { uri: URI, tool: "task_get", arguments: { slug: "tk-a" } },
		},
	);
});

test("anything else from the page is answered with an error or ignored", () => {
	for (const kind of ["run", "release", undefined]) {
		assert.deepEqual(relayRequest({ witan: "request", id: 3, kind }, URI), {
			id: 3,
			error: "witan: the page sent a request this extension does not relay.",
		});
	}
	assert.ok(
		"error" in
			(relayRequest(
				{ witan: "request", id: 4, kind: "read", tool: "task_get", args: [] },
				URI,
			) ?? {}),
	);
	for (const message of [null, "x", { witan: "request" }, { id: 1 }]) {
		assert.equal(relayRequest(message, URI), undefined);
	}
});
