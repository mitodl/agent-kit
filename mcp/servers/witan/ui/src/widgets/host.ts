import type { Transport } from "@modelcontextprotocol/client";
import { App, applyDocumentTheme } from "@modelcontextprotocol/ext-apps";
import { html, render, type TemplateResult } from "lit-html";
import { errorBox } from "../chrome.js";
import { isLinkable } from "../format.js";
import { DEFAULT_ROUTE, type Route } from "../route.js";
import { unwrap } from "../unwrap.js";
import "../style.css";

/**
 * The shared half of every MCP Apps widget (spec §7).
 *
 * A widget is a presentation of ONE tool result. The host calls the tool,
 * then hands this iframe the `CallToolResult` over `postMessage`; the widget
 * unwraps it with the same flags the page uses and draws it with the page's
 * own renderer. It calls nothing back. Claude Code renders no widgets at all
 * (anthropics/claude-code#95149), so anything a widget could only show by
 * issuing a second read is something most of our sessions would never see.
 */

/** What a widget's renderer is handed: the unwrapped value and the call's arguments. */
export interface WidgetInput<T> {
	value: T;
	/** The arguments the model called the tool with, `{}` until the host sends them. */
	args: Record<string, unknown>;
	route: Route;
	now: number;
}

/**
 * The route every widget renders under.
 *
 * All repos and closed tasks shown, because a widget draws whatever its call
 * returned: the scope was the caller's `repo` argument, and hiding closed rows
 * the call asked for would be the widget filtering a result it was handed.
 */
export const WIDGET_ROUTE: Route = { ...DEFAULT_ROUTE, closed: true };

/**
 * Draw `tool`'s result into `root` whenever the host sends one.
 *
 * `transport` is the host channel. Omitted, `App.connect` uses its
 * `postMessage` transport to the parent frame, which is the only one a
 * widget has in a real host; tests pass an in-memory pair instead.
 */
export function mountWidget<T>(
	tool: string,
	draw: (input: WidgetInput<T>) => TemplateResult,
	{
		root = document.body,
		transport,
	}: { root?: HTMLElement; transport?: Transport } = {},
): App {
	document.documentElement.classList.add("widget");
	let args: Record<string, unknown> = {};

	const show = (template: TemplateResult) =>
		render(html`<main class="widget-main">${template}</main>`, root);

	show(html`<p class="placeholder" aria-busy="true">Waiting for ${tool}…</p>`);

	const app = new App({ name: `witan-${tool}`, version: "1.0.0" });

	app.addEventListener("toolinput", (params) => {
		args = params.arguments ?? {};
	});

	app.addEventListener("toolresult", (result) => {
		if (result.isError) {
			const text = result.content
				?.flatMap((block) => (block.type === "text" ? [block.text] : []))
				.join("\n");
			show(errorBox(new Error(text || "The tool reported an error."), tool));
			return;
		}
		try {
			const value = unwrap<T>(tool, result.structuredContent);
			show(draw({ value, args, route: WIDGET_ROUTE, now: Date.now() }));
		} catch (error) {
			show(errorBox(error as Error, tool));
		}
	});

	// A cancelled call never sends a result, so without this the widget says
	// "Waiting for …" forever.
	app.addEventListener("toolcancelled", (params) => {
		show(
			errorBox(
				new Error(
					`The call was cancelled${params.reason ? `: ${params.reason}` : "."}`,
				),
				tool,
			),
		);
	});

	app.addEventListener("hostcontextchanged", (context) => {
		if (context.theme) {
			applyDocumentTheme(context.theme);
		}
	});

	root.addEventListener("click", (event) => onClick(app, event));

	app.connect(transport).then(
		() => {
			const theme = app.getHostContext()?.theme;
			if (theme) {
				applyDocumentTheme(theme);
			}
		},
		// No host answered the handshake, so no result will ever arrive. Saying
		// so beats "Waiting for …" forever.
		(error: Error) => show(errorBox(error, tool)),
	);

	return app;
}

/**
 * Keep every click inside the iframe from navigating it.
 *
 * The renderers are the page's, and their links are route fragments that mean
 * something only in the page. Inside a widget there is no router, so a
 * fragment link does nothing and is styled as text (`.widget` in style.css).
 * An external link, e.g. a project's PR, goes to the host through `openLink`,
 * which is how an app asks for a URL to be opened at all: a sandboxed iframe
 * cannot open one itself.
 */
function onClick(app: App, event: MouseEvent): void {
	const anchor = (event.target as Element | null)?.closest("a");
	if (!anchor) {
		return;
	}
	event.preventDefault();
	// The same test `uriFact` used to decide this was a link at all, so a
	// value drawn as one is never a link that silently does nothing (e.g.
	// `HTTPS://…`, which a hand-rolled scheme regex would miss).
	const href = anchor.getAttribute("href") ?? "";
	if (isLinkable(href)) {
		void app.openLink({ url: anchor.href });
	}
}
