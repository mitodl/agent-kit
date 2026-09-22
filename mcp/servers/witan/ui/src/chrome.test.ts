import { render } from "lit-html";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { emptyBox, errorBox, placeholderFor, readStatus } from "./chrome.js";
import type { Snapshot } from "./live.js";

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement("div");
	document.body.replaceChildren(root);
});

function snapshot<T>(overrides: Partial<Snapshot<T>> = {}): Snapshot<T> {
	return {
		data: null,
		error: null,
		loadedAt: null,
		loading: false,
		...overrides,
	};
}

const NOW = Date.parse("2026-09-22T12:00:00Z");

describe("readStatus", () => {
	it("shows when the data was read", () => {
		render(
			readStatus(
				snapshot({ data: [], loadedAt: Date.parse("2026-09-22T11:58:00Z") }),
				() => {},
				NOW,
			),
			root,
		);
		expect(root.textContent).toContain("read 2m ago");
	});

	it("marks a failed refresh over retained data", () => {
		render(
			readStatus(
				snapshot({
					data: [],
					error: new Error("server went away"),
					loadedAt: NOW - 60_000,
				}),
				() => {},
				NOW,
			),
			root,
		);

		const stale = root.querySelector(".stale");
		expect(stale?.textContent).toContain("stale");
		expect(stale?.getAttribute("title")).toBe("server went away");
	});

	it("keeps refresh reachable after a failed first read", () => {
		// The one moment someone most wants to retry is the one where there is
		// nothing on screen to retry from.
		const refresh = vi.fn();
		render(
			readStatus(snapshot({ error: new Error("nope") }), refresh, NOW),
			root,
		);

		root.querySelector("button")?.click();
		expect(refresh).toHaveBeenCalled();
	});
});

describe("placeholderFor", () => {
	it("returns nothing to render once a read has landed", () => {
		expect(
			placeholderFor(snapshot({ data: [1], loadedAt: NOW }), "tasks"),
		).toBeNull();
	});

	it("renders the body for a result that is legitimately null", () => {
		// `task_get` of a missing slug answers `null` as its VALUE. Keyed on the
		// data rather than on `loadedAt`, this would say "Reading…" forever.
		expect(
			placeholderFor(snapshot({ data: null, loadedAt: NOW }), "a task"),
		).toBeNull();
	});

	it("keeps stale data on screen rather than replacing it", () => {
		// ★ The blanking spec §6.1 rules out. A snapshot with data AND an error
		// still renders its body; the error is reported by the status line.
		expect(
			placeholderFor(
				snapshot({ data: [1], loadedAt: NOW, error: new Error("boom") }),
				"tasks",
			),
		).toBeNull();
	});

	it("reports an error when there is nothing to fall back on", () => {
		const box = placeholderFor(snapshot({ error: new Error("boom") }), "tasks");
		if (!box) {
			throw new Error("expected an error placeholder");
		}
		render(box, root);
		expect(root.querySelector('[role="alert"]')?.textContent).toContain("boom");
	});

	it("says it is reading before the first result", () => {
		const box = placeholderFor(snapshot(), "tasks");
		if (!box) {
			throw new Error("expected a loading placeholder");
		}
		render(box, root);
		expect(root.textContent).toContain("Reading tasks…");
	});
});

describe("emptyBox and errorBox", () => {
	it("are different states, worded differently", () => {
		render(emptyBox("No projects in the graph."), root);
		const empty = root.textContent ?? "";

		render(errorBox(new Error("connection refused"), "projects"), root);
		const failed = root.textContent ?? "";

		expect(empty).toContain("No projects");
		expect(failed).toContain("Could not read projects");
		expect(failed).toContain("connection refused");
	});
});
