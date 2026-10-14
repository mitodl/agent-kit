import { readdirSync } from "node:fs";
import { build } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

/**
 * markdown-it declares no `sideEffects` and runs code at module top level, so
 * rollup keeps it and its dependencies (about 40KB gzipped) in any widget whose
 * views import `markdown.ts`, including the ones that never render prose
 * (`recall`, `task_list`). None of them has a
 * side-effect-only import, so marking them pure drops them only where nothing
 * calls them; `workflow_project_status` renders a session summary and keeps
 * the parser.
 */
const unusedMarkdownIt = {
	name: "drop-unused-markdown-it",
	transform(code, id) {
		if (
			/\/node_modules\/(markdown-it|linkify-it|mdurl|uc\.micro|entities|punycode\.js)\//.test(
				id,
			)
		) {
			return { code, moduleSideEffects: false };
		}
		return null;
	},
};

/**
 * Build each MCP Apps widget (spec §7) into one self-contained HTML file.
 *
 * One build per widget because `vite-plugin-singlefile` inlines a single
 * entry's whole graph into its HTML, and rollup cannot do that for several
 * inputs at once. The host serves a widget as a `ui://` resource into a
 * sandboxed iframe whose default CSP is `connect-src 'none'`, so there is no
 * origin a separate script or stylesheet could be fetched from.
 *
 * Runs after the page build, into the same `ui_dist`, so the wheel and the
 * image carry the widgets with no packaging change. `emptyOutDir` is off for
 * that reason: on, each widget would delete the page and the widget before it.
 *
 * Every `widgets/*.html` is built; the server binds a tool to its widget only
 * when this file exists (`witan/ui_widgets.py`), so adding one here does
 * nothing until the server names it too.
 */
const widgets = readdirSync("widgets").filter((name) => name.endsWith(".html"));

for (const widget of widgets) {
	await build({
		configFile: false,
		logLevel: "warn",
		plugins: [unusedMarkdownIt, viteSingleFile()],
		build: {
			outDir: "../witan/ui_dist",
			emptyOutDir: false,
			rollupOptions: { input: `widgets/${widget}` },
		},
	});
}
