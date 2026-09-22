import { render } from "lit-html";
import { beforeEach, describe, expect, it } from "vitest";
import { placeholder, shell, VIEWS, viewFromHash } from "./shell.js";

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement("div");
	document.body.replaceChildren(root);
});

describe("viewFromHash", () => {
	it("resolves a known fragment", () => {
		expect(viewFromHash("#board")).toBe("board");
	});

	it("tolerates a fragment with no leading hash", () => {
		expect(viewFromHash("board")).toBe("board");
	});

	it("falls back to the first view for an unknown fragment", () => {
		// A stale bookmark has to land somewhere: there is no 404 to serve from a
		// URL fragment, and an unmatched id would otherwise render an empty frame.
		expect(viewFromHash("#no-such-view")).toBe(VIEWS[0].id);
	});

	it("falls back for an empty hash", () => {
		expect(viewFromHash("")).toBe(VIEWS[0].id);
	});
});

describe("shell", () => {
	it("renders a link per view", () => {
		render(shell("projects", placeholder()), root);

		const labels = [...root.querySelectorAll("nav a")].map((a) =>
			a.textContent?.trim(),
		);
		expect(labels).toEqual(VIEWS.map((view) => view.label));
	});

	it("marks exactly the active view as current", () => {
		render(shell("board", placeholder()), root);

		const current = [...root.querySelectorAll('nav a[aria-current="page"]')];
		expect(current.map((a) => a.textContent?.trim())).toEqual(["Board"]);
	});

	it("renders the body into main", () => {
		render(shell("projects", placeholder()), root);

		expect(root.querySelector("main")?.textContent).toContain(
			"No views are built yet",
		);
	});
});
