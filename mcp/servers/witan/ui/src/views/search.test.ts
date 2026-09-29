import { render } from "lit-html";
import { beforeEach, describe, expect, it } from "vitest";
import taskListFixture from "../../fixtures/task_list.json" with {
	type: "json",
};
import taskSearchFixture from "../../fixtures/task_search.json" with {
	type: "json",
};
import projectListFixture from "../../fixtures/workflow_project_list.json" with {
	type: "json",
};
import { DEFAULT_ROUTE, type Route } from "../route.js";
import type {
	TaskRow,
	TaskSearchRow,
	WorkflowProjectSummary,
} from "../types.js";
import { unwrap } from "../unwrap.js";
import {
	matchesText,
	SEARCH_LIMIT,
	searchView,
	taskMatches,
} from "./search.js";

const projects = unwrap<WorkflowProjectSummary[]>(
	"workflow_project_list",
	projectListFixture,
);
const hits = unwrap<TaskSearchRow[]>("task_search", taskSearchFixture);
const tasks = unwrap<TaskRow[]>("task_list", taskListFixture);

const project = projects[0];
const hit = hits[0];
if (!project || !hit) {
	throw new Error(
		"the project list and task search fixtures must not be empty",
	);
}

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement("div");
	document.body.replaceChildren(root);
});

function search(find: string, patch: Partial<Route> = {}): Route {
	return { ...DEFAULT_ROUTE, view: "search", find, ...patch };
}

describe("matchesText", () => {
	it("needs every word, in any order and any case", () => {
		expect(matchesText(["Search in the witan UI"], "ui SEARCH")).toBe(true);
		expect(matchesText(["Search in the witan UI"], "ui board")).toBe(false);
	});

	it("matches a word inside a slug", () => {
		expect(matchesText(["tk-right-size-3b8d0f"], "3b8d0f")).toBe(true);
	});

	it("skips absent fields rather than matching their string form", () => {
		expect(matchesText([null, undefined, "title"], "null")).toBe(false);
	});
});

describe("taskMatches", () => {
	it("matches a task_list row on its title", () => {
		const row = tasks[0];
		if (!row) {
			throw new Error("task_list fixture is empty");
		}
		expect(taskMatches(row, row.title.split(" ")[0] ?? "")).toBe(true);
		expect(taskMatches(row, "no-such-word-anywhere")).toBe(false);
	});
});

describe("searchView", () => {
	it("lists the projects whose title matches, linking to their rollup", () => {
		render(
			searchView({ tasks: [], capped: false }, projects, search(project.title)),
			root,
		);

		const link = root.querySelector<HTMLAnchorElement>(
			`a[href*="project=${project.slug}"]`,
		);
		expect(link?.textContent?.trim()).toBe(project.title);
		// The rollup is not filtered by the search, so the link drops it.
		expect(link?.getAttribute("href")).toBe(
			`#projects?project=${project.slug}`,
		);
	});

	it("leaves out projects that do not match", () => {
		render(
			searchView(
				{ tasks: [], capped: false },
				projects,
				search("no-such-word-anywhere"),
			),
			root,
		);
		expect(root.textContent).toContain("No active projects matched.");
	});

	it("opens a task in the panel, keeping the search behind it", () => {
		render(
			searchView({ tasks: hits, capped: false }, projects, search("read")),
			root,
		);

		const link = [...root.querySelectorAll<HTMLAnchorElement>("a")].find(
			(a) => a.textContent?.trim() === hit.title,
		);
		expect(link?.getAttribute("href")).toBe(
			`#search?slug=${hit.slug}&find=read`,
		);
	});

	it("names a task's project by its title when the list has it", () => {
		render(
			searchView({ tasks: hits, capped: false }, projects, search("read")),
			root,
		);
		const cell = root
			.querySelector(`a[href*="slug=${hit.slug}"]`)
			?.closest("tr")
			?.querySelector("td:nth-child(4)");
		expect(hit.project_slug).toBe(project.slug);
		expect(cell?.textContent?.trim()).toBe(project.title);
	});

	it("names a project outside the list by its slug", () => {
		render(
			searchView({ tasks: hits, capped: false }, [], search("read")),
			root,
		);
		const cell = root
			.querySelector(`a[href*="slug=${hit.slug}"]`)
			?.closest("tr")
			?.querySelector("td:nth-child(4)");
		expect(cell?.textContent?.trim()).toBe(hit.project_slug);
	});

	it("hides closed tasks until Closed is ticked", () => {
		const closed = { ...hit, slug: "tk-closed", status: "closed" as const };
		render(
			searchView({ tasks: [closed], capped: false }, projects, search("read")),
			root,
		);
		expect(root.textContent).toContain("No open tasks matched.");

		render(
			searchView(
				{ tasks: [closed], capped: false },
				projects,
				search("read", { closed: true }),
			),
			root,
		);
		expect(root.querySelector('a[href*="slug=tk-closed"]')).not.toBeNull();
	});

	it("says when task search hit its cap", () => {
		render(
			searchView({ tasks: hits, capped: true }, projects, search("read")),
			root,
		);
		expect(root.textContent).toContain(`${SEARCH_LIMIT} best matches`);
	});
});
