import { html, nothing, render } from "lit-html";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_ROUTE, type Route } from "./route.js";
import { detailPanel, type ShellProps, shell, VIEWS } from "./shell.js";

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement("div");
	document.body.replaceChildren(root);
});

function frame(overrides: Partial<ShellProps> = {}): ShellProps {
	return {
		route: DEFAULT_ROUTE,
		repos: [],
		projects: [],
		status: html`<span>status</span>`,
		body: html`<p>body</p>`,
		panel: nothing,
		onNavigate: () => {},
		...overrides,
	};
}

describe("shell", () => {
	it("renders a link per view", () => {
		render(shell(frame()), root);

		const labels = [...root.querySelectorAll("nav a")].map((a) =>
			a.textContent?.trim(),
		);
		expect(labels).toEqual(VIEWS.map((view) => view.label));
	});

	it("marks exactly the active view as current", () => {
		render(shell(frame({ route: { ...DEFAULT_ROUTE, view: "board" } })), root);

		const current = [...root.querySelectorAll('nav a[aria-current="page"]')];
		expect(current.map((a) => a.textContent?.trim())).toEqual(["Board"]);
	});

	it("carries the filters through every tab link", () => {
		// A person who has narrowed to one project and switches tabs should stay
		// narrowed; that is what makes the tabs views OF something rather than
		// six independent pages.
		const route: Route = { ...DEFAULT_ROUTE, project: "wp-x", closed: true };
		render(shell(frame({ route })), root);

		const board = root.querySelector<HTMLAnchorElement>('nav a[href*="board"]');
		expect(board?.getAttribute("href")).toBe("#board?project=wp-x&closed=1");
	});

	it("renders the body into main", () => {
		render(shell(frame()), root);
		expect(root.querySelector("main")?.textContent).toContain("body");
	});

	it("offers every known repo plus the all-repos scope", () => {
		render(
			shell(frame({ repos: ["https://github.com/mitodl/agent-kit"] })),
			root,
		);

		const options = [...root.querySelectorAll("select")][0];
		expect(
			[...(options?.options ?? [])].map((o) => o.textContent?.trim()),
		).toEqual(["All repos", "mitodl/agent-kit"]);
	});

	it("drops the project selection when the repo changes", () => {
		// A project belongs to a repo scope, so carrying the selection across
		// would filter the list to a project no longer in it and show nothing.
		const onNavigate = vi.fn();
		const route: Route = { ...DEFAULT_ROUTE, project: "wp-x", slug: "tk-y" };
		render(
			shell(
				frame({ route, onNavigate, repos: ["https://github.com/mitodl/hq"] }),
			),
			root,
		);

		const select = root.querySelectorAll("select")[0] as HTMLSelectElement;
		select.value = "https://github.com/mitodl/hq";
		select.dispatchEvent(new Event("change"));

		expect(onNavigate).toHaveBeenCalledWith({
			repo: "https://github.com/mitodl/hq",
			project: null,
			slug: null,
		});
	});

	it("reports a project selection", () => {
		const onNavigate = vi.fn();
		render(
			shell(
				frame({
					onNavigate,
					projects: [{ value: "wp-x", label: "Witan UI" }],
				}),
			),
			root,
		);

		const select = root.querySelectorAll("select")[1] as HTMLSelectElement;
		select.value = "wp-x";
		select.dispatchEvent(new Event("change"));

		expect(onNavigate).toHaveBeenCalledWith({ project: "wp-x", slug: null });
	});

	it("reports the closed toggle", () => {
		const onNavigate = vi.fn();
		render(shell(frame({ onNavigate })), root);

		const toggle = root.querySelector<HTMLInputElement>(
			'.closed-toggle input[type="checkbox"]',
		);
		if (!toggle) {
			throw new Error("the closed toggle is missing");
		}
		toggle.checked = true;
		toggle.dispatchEvent(new Event("change"));

		expect(onNavigate).toHaveBeenCalledWith({ closed: true });
	});
});

describe("detailPanel", () => {
	it("closes through the route, not a handler", () => {
		// Same mechanism as the Escape key and the back button, so the three
		// cannot disagree about what is open.
		const route: Route = { ...DEFAULT_ROUTE, project: "wp-x", slug: "tk-y" };
		render(detailPanel(route, "A task", html`<p>detail</p>`), root);

		expect(
			root.querySelector<HTMLAnchorElement>(".close")?.getAttribute("href"),
		).toBe("#projects?project=wp-x");
	});

	it("gives the close control an accessible name", () => {
		// `title` does not supply the accessible name when the element has text,
		// so a screen reader announced the glyph "×" and nothing else.
		render(detailPanel(DEFAULT_ROUTE, "A task", html`<p>detail</p>`), root);

		expect(root.querySelector(".close")?.getAttribute("aria-label")).toBe(
			"Close details",
		);
	});

	it("carries the panel's own read status when given one", () => {
		render(
			detailPanel(
				DEFAULT_ROUTE,
				"A task",
				html`<p>detail</p>`,
				html`<span class="stale">stale</span>`,
			),
			root,
		);

		expect(root.querySelector(".detail-panel header .stale")).not.toBeNull();
	});

	it("is focusable, so opening it can move focus into it", () => {
		render(detailPanel(DEFAULT_ROUTE, "A task", html`<p>detail</p>`), root);
		expect(root.querySelector(".detail-panel")?.getAttribute("tabindex")).toBe(
			"-1",
		);
	});
});
