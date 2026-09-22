import { render } from "lit-html";
import { beforeEach, describe, expect, it } from "vitest";
import taskListFixture from "../../fixtures/task_list.json" with {
	type: "json",
};
import projectListFixture from "../../fixtures/workflow_project_list.json" with {
	type: "json",
};
import { DEFAULT_ROUTE, type Route } from "../route.js";
import type { TaskRow, WorkflowProjectSummary } from "../types.js";
import { unwrap } from "../unwrap.js";
import { components, layout, type Waves, waves, wavesPicker } from "./waves.js";

/**
 * The waves view, from rows DERIVED from the recorded `task_list` result, so
 * every field the server sends is present and a renamed one still fails here.
 */

const tasks = unwrap<TaskRow[]>("task_list", taskListFixture);
const projects = unwrap<WorkflowProjectSummary[]>(
	"workflow_project_list",
	projectListFixture,
);

const PROJECT = "wp-waves";
const route: Route = { ...DEFAULT_ROUTE, view: "waves", project: PROJECT };

function task(slug: string, patch: Partial<TaskRow> = {}): TaskRow {
	const base = tasks[0];
	if (!base) {
		throw new Error("task_list fixture is empty");
	}
	return {
		...base,
		slug,
		title: slug,
		status: "open",
		priority: "p2",
		project_slug: PROJECT,
		blocked_by: null,
		assignee: null,
		claimed_at: null,
		lease_expired: undefined,
		...patch,
	};
}

/** `task_ready` for `rows`, by its rule, so a test states only what it is about. */
function data(rows: TaskRow[], patch: Partial<Waves> = {}): Waves {
	const open = new Set(
		rows.filter((row) => row.status !== "closed").map((row) => row.slug),
	);
	return {
		tasks: rows,
		ready: rows.filter(
			(row) =>
				row.status !== "closed" &&
				(row.status !== "in_progress" || row.lease_expired) &&
				!(row.blocked_by ?? []).some((slug) => open.has(slug)),
		),
		outside: [],
		...patch,
	};
}

/** Rendered text with whitespace collapsed, as a reader sees it. */
function text(root: HTMLElement): string {
	return (root.textContent ?? "").replace(/\s+/g, " ");
}

function waveOf(plan: ReturnType<typeof layout>): Record<string, number> {
	return Object.fromEntries(
		plan.nodes.map((node) => [node.task.slug, node.wave]),
	);
}

describe("components", () => {
	it("orders components blockers first", () => {
		const next = new Map([
			["tk-a", ["tk-b"]],
			["tk-b", ["tk-c"]],
		]);
		expect(components(["tk-c", "tk-b", "tk-a"], next)).toEqual([
			["tk-a"],
			["tk-b"],
			["tk-c"],
		]);
	});

	it("collapses a cycle into one component", () => {
		const next = new Map([
			["tk-a", ["tk-b"]],
			["tk-b", ["tk-a", "tk-c"]],
		]);
		expect(components(["tk-a", "tk-b", "tk-c"], next)).toEqual([
			["tk-a", "tk-b"],
			["tk-c"],
		]);
	});

	it("walks a chain far longer than the call stack is deep", () => {
		// A recursive visit threw RangeError here before anything was drawn.
		const length = 50_000;
		const slugs = Array.from({ length }, (_, at) => `tk-${at}`);
		const next = new Map(
			slugs.slice(0, -1).map((slug, at) => [slug, [slugs[at + 1] as string]]),
		);
		const found = components(slugs, next);

		expect(found).toHaveLength(length);
		expect(found[0]).toEqual(["tk-0"]);
		expect(found.at(-1)).toEqual([`tk-${length - 1}`]);
	});
});

describe("layout", () => {
	it("draws a project with no dependencies as a single wave", () => {
		const plan = layout(data([task("tk-a"), task("tk-b"), task("tk-c")]));

		expect(plan.waves).toBe(1);
		expect(Object.values(waveOf(plan))).toEqual([0, 0, 0]);
		expect(plan.critical).toEqual([]);
		expect(plan.edges).toEqual([]);
		expect(plan.unblocksMost).toBeNull();
	});

	it("places each task one wave past its deepest open blocker", () => {
		const plan = layout(
			data([
				task("tk-a"),
				task("tk-b", { blocked_by: ["tk-a"] }),
				task("tk-c", { blocked_by: ["tk-b"] }),
				task("tk-d", { blocked_by: ["tk-a", "tk-c"] }),
				task("tk-e"),
			]),
		);

		expect(waveOf(plan)).toEqual({
			"tk-a": 0,
			"tk-e": 0,
			"tk-b": 1,
			"tk-c": 2,
			"tk-d": 3,
		});
		expect(plan.waves).toBe(4);
	});

	it("traces the longest chain and marks its edges", () => {
		const plan = layout(
			data([
				task("tk-a"),
				task("tk-b", { blocked_by: ["tk-a"] }),
				task("tk-c", { blocked_by: ["tk-b"] }),
				task("tk-d", { blocked_by: ["tk-a"] }),
			]),
		);

		expect(plan.critical).toEqual(["tk-a", "tk-b", "tk-c"]);
		expect(
			plan.edges
				.filter((edge) => edge.critical)
				.map((edge) => `${edge.from}>${edge.to}`)
				.sort(),
		).toEqual(["tk-a>tk-b", "tk-b>tk-c"]);
		expect(
			plan.nodes.filter((node) => node.critical).map((node) => node.task.slug),
		).toEqual(["tk-a", "tk-b", "tk-c"]);
	});

	it("names the wave-0 task with the most work waiting on it", () => {
		const plan = layout(
			data([
				task("tk-a"),
				task("tk-b", { blocked_by: ["tk-a"] }),
				task("tk-c", { blocked_by: ["tk-b"] }),
				task("tk-x"),
				task("tk-y", { blocked_by: ["tk-x"] }),
			]),
		);

		expect(plan.unblocksMost?.task.slug).toBe("tk-a");
		// Transitive: tk-c waits on tk-a through tk-b.
		expect(plan.unblocksMost?.downstream).toBe(2);
	});

	it("does not wait on a closed blocker", () => {
		const plan = layout(
			data([
				task("tk-a", { status: "closed" }),
				task("tk-b", { blocked_by: ["tk-a"] }),
			]),
		);

		expect(waveOf(plan)).toEqual({ "tk-b": 0 });
		expect(plan.edges).toEqual([]);
	});

	it("does not wait on a blocker that no longer exists", () => {
		// `task_ready`'s resolver: a missing blocker holds nothing back.
		const plan = layout(data([task("tk-b", { blocked_by: ["tk-gone"] })]));

		expect(waveOf(plan)).toEqual({ "tk-b": 0 });
		expect(plan.disagree).toEqual([]);
	});

	it("draws an open blocker from another project as a stub", () => {
		const elsewhere = task("tk-elsewhere", { project_slug: "wp-other" });
		const plan = layout({
			tasks: [task("tk-b", { blocked_by: ["tk-elsewhere"] })],
			ready: [],
			outside: [elsewhere],
		});

		const stub = plan.nodes.find((node) => node.task.slug === "tk-elsewhere");
		expect(stub?.outside).toBe(true);
		expect(stub?.wave).toBe(0);
		expect(waveOf(plan)["tk-b"]).toBe(1);
		expect(plan.edges).toEqual([
			{ from: "tk-elsewhere", to: "tk-b", critical: true, cycle: false },
		]);
		expect(plan.disagree).toEqual([]);
		// The stub is not the project's own work to land first.
		expect(plan.unblocksMost).toBeNull();
	});

	it("reports a cycle instead of looping on it", () => {
		const plan = layout(
			data([
				task("tk-a", { blocked_by: ["tk-b"] }),
				task("tk-b", { blocked_by: ["tk-a"] }),
				task("tk-c", { blocked_by: ["tk-b"] }),
				task("tk-d"),
			]),
		);

		expect(plan.cycles).toEqual([["tk-a", "tk-b"]]);
		expect(waveOf(plan)).toEqual({
			"tk-a": 0,
			"tk-b": 0,
			"tk-d": 0,
			"tk-c": 1,
		});
		expect(
			plan.nodes
				.filter((node) => node.cycle)
				.map((node) => node.task.slug)
				.sort(),
		).toEqual(["tk-a", "tk-b"]);
		expect(
			plan.edges
				.filter((edge) => edge.cycle)
				.map((edge) => `${edge.from}>${edge.to}`)
				.sort(),
		).toEqual(["tk-a>tk-b", "tk-b>tk-a"]);
		// A cycle member can never land first.
		expect(plan.unblocksMost).toBeNull();
		expect(plan.disagree).toEqual([]);
	});

	it("names the cycle member a critical path leaves through", () => {
		// tk-a feeds tk-c, and the path enters the cycle at tk-b (from tk-d).
		// Naming tk-b alone would claim a tk-a → tk-b edge that does not exist.
		const plan = layout(
			data([
				task("tk-a"),
				task("tk-b", { blocked_by: ["tk-c"] }),
				task("tk-c", { blocked_by: ["tk-a", "tk-b"] }),
				task("tk-d", { blocked_by: ["tk-b"] }),
			]),
		);

		expect(plan.critical).toEqual(["tk-a", "tk-c", "tk-b", "tk-d"]);
		expect(
			plan.edges
				.filter((edge) => edge.critical)
				.map((edge) => `${edge.from}>${edge.to}`)
				.sort(),
		).toEqual(["tk-a>tk-c", "tk-b>tk-d", "tk-c>tk-b"]);
	});

	it("walks every member between where a path enters a cycle and leaves it", () => {
		// x → z → y → x, entered at x from a, left from y to d. Naming x and y
		// alone would claim an x → y edge and drop z.
		const plan = layout(
			data([
				task("tk-a"),
				task("tk-x", { blocked_by: ["tk-a", "tk-y"] }),
				task("tk-z", { blocked_by: ["tk-x"] }),
				task("tk-y", { blocked_by: ["tk-z"] }),
				task("tk-d", { blocked_by: ["tk-y"] }),
			]),
		);

		expect(plan.critical).toEqual(["tk-a", "tk-x", "tk-z", "tk-y", "tk-d"]);
		expect(
			plan.edges
				.filter((edge) => edge.critical)
				.map((edge) => `${edge.from}>${edge.to}`)
				.sort(),
		).toEqual(["tk-a>tk-x", "tk-x>tk-z", "tk-y>tk-d", "tk-z>tk-y"]);
	});

	it("reports a task that blocks itself", () => {
		const plan = layout(data([task("tk-a", { blocked_by: ["tk-a"] })]));

		expect(plan.cycles).toEqual([["tk-a"]]);
	});

	it("agrees with task_ready when a held task sits in wave 0", () => {
		const plan = layout(
			data([
				task("tk-held", {
					status: "in_progress",
					assignee: "someone@mit.edu#abc",
					lease_expired: false,
				}),
				task("tk-lapsed", { status: "in_progress", lease_expired: true }),
			]),
		);

		expect(plan.disagree).toEqual([]);
		expect(
			plan.nodes.map((node) => [node.task.slug, node.wave, node.ready]),
		).toEqual([
			["tk-held", 0, false],
			["tk-lapsed", 0, true],
		]);
	});

	it("lists where task_ready and wave 0 disagree, in both directions", () => {
		const a = task("tk-a");
		const b = task("tk-b", { blocked_by: ["tk-a"] });
		const plan = layout({ tasks: [a, b], ready: [b], outside: [] });

		expect(plan.disagree.map((row) => row.slug).sort()).toEqual([
			"tk-a",
			"tk-b",
		]);
	});

	it("lists a ready task the chart has closed", () => {
		const closed = task("tk-a", { status: "closed" });
		const plan = layout({
			tasks: [closed],
			ready: [task("tk-a")],
			outside: [],
		});

		expect(plan.disagree.map((row) => row.slug)).toEqual(["tk-a"]);
	});

	it("orders rows by wave, the critical path first, then priority", () => {
		const plan = layout(
			data([
				task("tk-z", { priority: "p0" }),
				task("tk-a", { priority: "p3" }),
				task("tk-b", { blocked_by: ["tk-a"] }),
			]),
		);

		expect(plan.nodes.map((node) => node.task.slug)).toEqual([
			"tk-a",
			"tk-z",
			"tk-b",
		]);
	});
});

describe("waves", () => {
	let root: HTMLElement;

	beforeEach(() => {
		root = document.createElement("div");
	});

	it("draws one label and one bar per row, and an edge per open blocker", () => {
		render(
			waves(
				data([
					task("tk-a"),
					task("tk-b", { blocked_by: ["tk-a"] }),
					task("tk-c", { blocked_by: ["tk-a"] }),
				]),
				route,
			),
			root,
		);

		const labels = [...root.querySelectorAll("ol.wv-labels li")].map((li) =>
			li.getAttribute("data-slug"),
		);
		expect(labels).toEqual(["tk-a", "tk-b", "tk-c"]);
		expect(root.querySelectorAll(".wv-track rect.wv-bar")).toHaveLength(3);
		expect(root.querySelectorAll("line.wv-edge")).toHaveLength(2);
		expect(text(root)).toContain("2 waves of work remain");
		expect(text(root)).toContain("unblocks 2 tasks");
	});

	it("puts each bar in its wave's column", () => {
		render(
			waves(
				data([task("tk-a"), task("tk-b", { blocked_by: ["tk-a"] })]),
				route,
			),
			root,
		);

		const x = [...root.querySelectorAll(".wv-track rect.wv-bar")].map((rect) =>
			Number.parseFloat(rect.getAttribute("x") ?? ""),
		);
		expect(x[0]).toBeLessThan(50);
		expect(x[1]).toBeGreaterThan(50);
	});

	it("marks ready, held and critical bars by class", () => {
		render(
			waves(
				data([
					task("tk-a"),
					task("tk-b", { blocked_by: ["tk-a"] }),
					task("tk-held", { status: "in_progress", lease_expired: false }),
				]),
				route,
			),
			root,
		);

		const cls = (slug: string) => {
			const row = [...root.querySelectorAll("ol.wv-labels li")].findIndex(
				(li) => li.getAttribute("data-slug") === slug,
			);
			return root
				.querySelectorAll(".wv-track rect.wv-bar")
				[row]?.getAttribute("class");
		};
		expect(cls("tk-a")).toBe("wv-bar ready critical");
		expect(cls("tk-held")).toBe("wv-bar held");
		expect(cls("tk-b")).toBe("wv-bar critical");
	});

	it("says so when nothing waits on anything", () => {
		render(waves(data([task("tk-a")]), route), root);

		expect(text(root)).toContain("1 wave of work remain");
		expect(text(root)).toContain("No task waits on another");
	});

	it("flags a cycle as an alert naming its members", () => {
		render(
			waves(
				data([
					task("tk-a", { blocked_by: ["tk-b"] }),
					task("tk-b", { blocked_by: ["tk-a"] }),
				]),
				route,
			),
			root,
		);

		// Down the gutter, not back across the two bars.
		for (const line of root.querySelectorAll("line.wv-edge.cycle")) {
			expect(line.getAttribute("x1")).toBe(line.getAttribute("x2"));
		}
		const alert = root.querySelector('[role="alert"]');
		expect(alert?.textContent).toContain("cycle");
		expect(alert?.querySelectorAll("a")).toHaveLength(2);
		expect(text(root)).not.toContain("No task waits on another");
	});

	it("says a project with nothing open is empty rather than drawing an axis", () => {
		render(waves(data([task("tk-a", { status: "closed" })]), route), root);

		expect(text(root)).toContain("Nothing is open in this project");
		expect(root.querySelector("svg")).toBeNull();
	});

	it("keeps the race note when the only task closed between the reads", () => {
		const closed = task("tk-a", { status: "closed" });
		render(
			waves({ tasks: [closed], ready: [task("tk-a")], outside: [] }, route),
			root,
		);

		expect(text(root)).toContain("Nothing is open in this project");
		expect(text(root)).toContain("task_ready and this chart disagree on tk-a");
	});

	it("links every task to the detail panel", () => {
		render(waves(data([task("tk-a")]), route), root);

		expect(
			root.querySelector("ol.wv-labels a")?.getAttribute("href"),
		).toContain("slug=tk-a");
	});

	it("positions nothing with inline styles, which /ui/'s CSP blocks", () => {
		render(
			waves(
				data([
					task("tk-a", { blocked_by: ["tk-b"] }),
					task("tk-b", { blocked_by: ["tk-a"] }),
					task("tk-c", { blocked_by: ["tk-b"] }),
				]),
				route,
			),
			root,
		);

		expect(root.querySelector("[style]")).toBeNull();
		// And reaches nothing off-origin: no script, image or external link.
		expect(
			root.querySelector("script, img, link, use[href^='http']"),
		).toBeNull();
	});
});

describe("wavesPicker", () => {
	it("links each project to its waves", () => {
		const root = document.createElement("div");
		render(wavesPicker(projects, { ...route, project: null }), root);

		const first = projects[0];
		expect(root.querySelector("a")?.getAttribute("href")).toBe(
			`#waves?project=${first?.slug}`,
		);
	});
});
