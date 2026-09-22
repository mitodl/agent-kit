import { render } from "lit-html";
import { beforeEach, describe, expect, it } from "vitest";
import taskGetFixture from "../../fixtures/task_get.json" with { type: "json" };
import { DEFAULT_ROUTE } from "../route.js";
import type { TaskDetail } from "../types.js";
import { unwrap } from "../unwrap.js";
import { taskDetail, taskMissing } from "./task-detail.js";

const task = unwrap<TaskDetail>("task_get", taskGetFixture);

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement("div");
	document.body.replaceChildren(root);
});

function text(): string {
	return root.textContent ?? "";
}

function hrefs(): (string | null)[] {
	return [...root.querySelectorAll("a")].map((a) => a.getAttribute("href"));
}

describe("taskDetail", () => {
	it("shows the node's own fields", () => {
		render(taskDetail(task, DEFAULT_ROUTE), root);

		expect(text()).toContain(task.slug);
		expect(text()).toContain(
			"task_list limit, list timestamps, task_get edges.",
		);
		expect(text()).toContain("mitodl/agent-kit");
		expect(text()).toContain("fixtures");
	});

	it("shows the comment thread, which is where corrections live", () => {
		render(taskDetail(task, DEFAULT_ROUTE), root);

		const comments = root.querySelectorAll(".comments li");
		expect(comments.length).toBe(task.comments.length);
		expect(comments[0]?.textContent).toContain(
			"Counting one past the cap is not the same as counting.",
		);
	});

	it("keeps comments in the order the tool returned them", () => {
		// Oldest first: a comment is how one agent tells the next that the task's
		// premise is wrong, and reversing them puts the correction first.
		const threaded: TaskDetail = {
			...task,
			comments: [
				{
					...(task.comments[0] as TaskDetail["comments"][number]),
					body: "earlier",
				},
				{
					...(task.comments[0] as TaskDetail["comments"][number]),
					body: "later",
				},
			],
		};
		render(taskDetail(threaded, DEFAULT_ROUTE), root);

		const bodies = [...root.querySelectorAll(".comments li .prose")].map(
			(p) => p.textContent,
		);
		expect(bodies).toEqual(["earlier", "later"]);
	});

	it("links the inverse edge the read-gap task added", () => {
		// `blocks` is what answers "what does closing this unblock", and it did
		// not exist on `task_get` before that task.
		render(taskDetail(task, DEFAULT_ROUTE), root);
		expect(hrefs()).toContain("#projects?slug=tk-fixture-004");
	});

	it("treats the repo fact as repo navigation, clearing project and panel", () => {
		// Same rule as the shell's repo select and the rollup's repo links.
		// Keeping them left the panel open over a project the filter no longer
		// selects, and re-read a rollup that `repo` does not narrow anyway.
		const route = { ...DEFAULT_ROUTE, project: "wp-x", slug: task.slug };
		render(taskDetail(task, route), root);

		const repoLink = root.querySelector<HTMLAnchorElement>(".facts a");
		expect(repoLink?.getAttribute("href")).toBe(
			"#projects?repo=https%3A%2F%2Fgithub.com%2Fmitodl%2Fagent-kit",
		);
	});

	it("links the parent and the project", () => {
		render(taskDetail(task, DEFAULT_ROUTE), root);

		expect(hrefs()).toContain(`#projects?slug=${task.parent_slug}`);
		expect(hrefs()).toContain(`#projects?project=${task.project_slug}`);
	});

	it("renders no empty scaffolding for a bare task", () => {
		// The task's acceptance criterion: a task with no comments, no blockers
		// and no project renders cleanly rather than showing empty sections.
		const bare: TaskDetail = {
			...task,
			description: null,
			resolution: null,
			project_slug: null,
			parent_slug: null,
			blocked_by: null,
			blocks: [],
			children: [],
			branches: [],
			symbol_refs: null,
			comments: [],
			tags: null,
			external_uri: null,
		};
		render(taskDetail(bare, DEFAULT_ROUTE), root);

		expect(root.querySelectorAll("section").length).toBe(0);
		expect(text()).not.toContain("Comments");
		expect(text()).not.toContain("Graph");
		// The identity and the timestamps stay: they are never absent.
		expect(text()).toContain(bare.slug);
		expect(text()).toContain("unassigned");
	});

	it("links an external URI and prints a non-URL one as text", () => {
		render(
			taskDetail(
				{ ...task, external_uri: "https://github.com/mitodl/hq/issues/1" },
				DEFAULT_ROUTE,
			),
			root,
		);
		expect(hrefs()).toContain("https://github.com/mitodl/hq/issues/1");

		render(
			taskDetail({ ...task, external_uri: "mitodl/hq#1" }, DEFAULT_ROUTE),
			root,
		);
		expect(text()).toContain("mitodl/hq#1");
		expect(hrefs()).not.toContain("mitodl/hq#1");
	});

	it("shows children with their status", () => {
		render(
			taskDetail(
				{
					...task,
					children: [{ slug: "tk-child", title: "A child", status: "open" }],
				},
				DEFAULT_ROUTE,
			),
			root,
		);

		const child = root.querySelector(".children li");
		expect(child?.textContent).toContain("A child");
		expect(child?.textContent).toContain("open");
	});

	it("links the branches carrying the work, with every field they return", () => {
		render(
			taskDetail(
				{
					...task,
					branches: [
						{
							slug: "cb-x",
							repo: "https://github.com/mitodl/agent-kit",
							branch: "witan-ui-drill-down",
							status: "active",
							updated_at: "2026-09-22T12:00:00",
						},
					],
				},
				DEFAULT_ROUTE,
			),
			root,
		);

		const branch = root.querySelector(".branches li");
		expect(branch?.textContent).toContain("witan-ui-drill-down");
		expect(branch?.textContent).toContain("active");
		// The fields the first cut dropped. `updated_at` reads as an age;
		// `slug` is `<repo>|<branch>`, so it goes in the title rather than
		// repeating two values already on the line.
		expect(branch?.textContent).toMatch(/\d+[mhd] ago|just now/);
		expect(branch?.getAttribute("title")).toBe("cb-x");
		// A branch is how a stale claim gets traced to a checkout, so it opens.
		expect(branch?.querySelector("a")?.getAttribute("href")).toBe(
			"https://github.com/mitodl/agent-kit/tree/witan-ui-drill-down",
		);
	});

	it("escapes a slash-bearing branch name into the tree URL", () => {
		render(
			taskDetail(
				{
					...task,
					branches: [
						{
							slug: "cb-y",
							repo: "https://github.com/mitodl/agent-kit",
							branch: "renovate/astral-sh-ruff",
							status: null,
							updated_at: null,
						},
					],
				},
				DEFAULT_ROUTE,
			),
			root,
		);

		// The separators stay separators; anything else in a segment does not.
		expect(root.querySelector(".branches a")?.getAttribute("href")).toBe(
			"https://github.com/mitodl/agent-kit/tree/renovate/astral-sh-ruff",
		);
	});

	it("renders a branch as text when its repo is not a URL", () => {
		render(
			taskDetail(
				{
					...task,
					branches: [
						{
							slug: "cb-z",
							repo: "some-local-thing",
							branch: "main",
							status: null,
							updated_at: null,
						},
					],
				},
				DEFAULT_ROUTE,
			),
			root,
		);

		expect(root.querySelector(".branches")?.textContent).toContain("main");
		expect(root.querySelector(".branches a")).toBeNull();
	});

	it("marks a comment body as prose, which is what preserves its line breaks", () => {
		// `white-space: pre-wrap` is carried by `.prose`, and a comment body IS
		// the paragraph rather than containing one. A body rendered without the
		// class collapses every multi-paragraph correction into a run-on block.
		render(taskDetail(task, DEFAULT_ROUTE), root);

		expect(root.querySelector(".comments li p.prose")).not.toBeNull();
	});

	it("marks a lapsed claim", () => {
		render(taskDetail({ ...task, lease_expired: true }, DEFAULT_ROUTE), root);
		expect(root.querySelector(".stale-claim")?.textContent).toContain("lapsed");
	});
});

describe("taskMissing", () => {
	it("reads as a stale link, not a failure", () => {
		render(taskMissing("tk-gone"), root);
		expect(text()).toContain("tk-gone");
		expect(text()).toContain("stale");
	});
});
