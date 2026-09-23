import { render } from "lit-html";
import { beforeEach, describe, expect, it, vi } from "vitest";
import graphFixture from "../../fixtures/graph.json" with { type: "json" };
import taskListFixture from "../../fixtures/task_list.json" with {
	type: "json",
};
import projectListFixture from "../../fixtures/workflow_project_list.json" with {
	type: "json",
};
import { DEFAULT_ROUTE, type Route } from "../route.js";
import type { TaskRow, WorkflowProjectSummary } from "../types.js";
import { unwrap } from "../unwrap.js";
import { type Canvas, CanvasHost } from "./canvas-host.js";
import {
	buildGraph,
	CLUSTER_ABOVE,
	type GraphData,
	graphView,
	type OnSelect,
	scopeTasks,
	type WorkflowGraph,
} from "./graph.js";

const tasks = unwrap<TaskRow[]>("task_list", taskListFixture);
const projects = unwrap<WorkflowProjectSummary[]>(
	"workflow_project_list",
	projectListFixture,
);
const route: Route = { ...DEFAULT_ROUTE, view: "graph" };

function task(slug: string, patch: Partial<TaskRow> = {}): TaskRow {
	const base = tasks[0];
	if (!base) {
		throw new Error("task_list fixture is empty");
	}
	return { ...base, slug, title: slug, blocked_by: null, ...patch };
}

describe("buildGraph", () => {
	it("draws the graph `witan graph` draws over the same reads", () => {
		// `fixtures/graph.json` is the Python `scope_tasks` + `build_graph` over
		// these two recorded results, so this is the port held to the CLI.
		const graph = buildGraph(projects, scopeTasks(projects, tasks));

		expect(
			graph.nodes.map(({ id, label, group, color, status }) => ({
				id,
				label,
				group,
				color,
				status,
			})),
		).toEqual(graphFixture.nodes);
		expect(graph.edges).toEqual(graphFixture.edges);
	});

	it("draws no edge to a node outside the graph", () => {
		const graph = buildGraph(
			[],
			[
				task("tk-a", {
					project_slug: "wp-gone",
					parent_slug: "tk-gone",
					blocked_by: ["tk-gone"],
				}),
			],
		);
		expect(graph.edges).toEqual([]);
	});

	it("truncates by code point, as Python slices, not by UTF-16 unit", () => {
		// 34 letters then an emoji: code point 35 is the emoji, which `slice`
		// would cut in half.
		const title = `${"a".repeat(34)}🚀tail`;
		const [node] = buildGraph(
			[],
			[task("tk-a", { title, priority: "p2" })],
		).nodes;
		expect(node?.label).toBe(`${"a".repeat(34)}🚀`);
	});

	it("marks p0 and p1 in the label, as the CLI does", () => {
		const graph = buildGraph(
			[],
			[
				task("tk-a", { priority: "p0", title: "Urgent" }),
				task("tk-b", { priority: "p2", title: "Later" }),
			],
		);
		expect(graph.nodes.map((node) => node.label)).toEqual([
			"[p0] Urgent",
			"Later",
		]);
	});
});

describe("buildGraph clusters", () => {
	const project = projects[0] as WorkflowProjectSummary;

	it("groups a project with its tasks, and projectless tasks by repo", () => {
		const graph = buildGraph(
			[project],
			[
				task("tk-in", { project_slug: project.slug }),
				task("tk-loose-a", {
					project_slug: null,
					repo: "https://github.com/o/a",
				}),
				task("tk-loose-b", {
					project_slug: null,
					repo: "https://github.com/o/a",
				}),
				// Its project is not in the graph, so it is projectless here.
				task("tk-orphan", { project_slug: "wp-gone", repo: null }),
			],
		);

		expect(
			graph.clusters.map(({ key, project: slug, tasks: members }) => ({
				key,
				slug,
				members,
			})),
		).toEqual([
			{ key: project.slug, slug: project.slug, members: ["tk-in"] },
			{
				key: "repo:https://github.com/o/a",
				slug: null,
				members: ["tk-loose-a", "tk-loose-b"],
			},
			{ key: "repo:", slug: null, members: ["tk-orphan"] },
		]);
		const byId = new Map(graph.nodes.map((node) => [node.id, node.cluster]));
		expect(byId.get(project.slug)).toBe(project.slug);
		expect(byId.get("tk-loose-b")).toBe("repo:https://github.com/o/a");
	});
});

describe("scopeTasks", () => {
	const project = projects[0] as WorkflowProjectSummary;

	it("drops closed tasks unless asked for them", () => {
		const rows = [task("tk-open"), task("tk-done", { status: "closed" })];
		expect(scopeTasks([project], rows).map((row) => row.slug)).toEqual([
			"tk-open",
		]);
		expect(scopeTasks([project], rows, true)).toHaveLength(2);
	});

	it("keeps a task with no project and drops one whose project is not listed", () => {
		const rows = [
			task("tk-loose", { project_slug: null }),
			task("tk-elsewhere", { project_slug: "wp-not-listed" }),
		];
		expect(scopeTasks([project], rows).map((row) => row.slug)).toEqual([
			"tk-loose",
		]);
	});
});

/** A canvas that records what it was handed, since jsdom cannot draw one. */
function fakeCanvas() {
	const canvas = {
		update: vi.fn(),
		destroy: vi.fn(),
	} satisfies Canvas<WorkflowGraph>;
	const mount = vi.fn(async () => canvas);
	return { canvas, mount };
}

describe("graphView", () => {
	let root: HTMLElement;

	beforeEach(() => {
		root = document.createElement("div");
		document.body.replaceChildren(root);
	});

	const data: GraphData = { projects, tasks, truncated: false };

	it("mounts one canvas and feeds it every render", async () => {
		const { canvas, mount } = fakeCanvas();
		const host = new CanvasHost<WorkflowGraph, OnSelect>(
			mount,
			() => {},
			() => {},
		);

		render(graphView(data, route, host), root);
		await vi.waitFor(() => expect(canvas.update).toHaveBeenCalledTimes(1));
		render(graphView(data, { ...route, slug: "tk-x" }, host), root);

		// The same container, so the same network: a remount per poll would
		// re-run the layout every 30 seconds.
		expect(mount).toHaveBeenCalledTimes(1);
		expect(canvas.update).toHaveBeenLastCalledWith(expect.anything(), "tk-x");
	});

	it("counts what it draws and shows the key", () => {
		const { mount } = fakeCanvas();
		render(
			graphView(
				data,
				route,
				new CanvasHost<WorkflowGraph, OnSelect>(
					mount,
					() => {},
					() => {},
				),
			),
			root,
		);
		const text = (root.textContent ?? "").replace(/\s+/g, " ");
		expect(text).toContain("1 project");
		expect(text).toContain("4 tasks");
		expect(text).toContain("7 edges");
		expect(root.querySelector(".gr-legend")).not.toBeNull();
	});

	it("lists every node as a link, for a keyboard or a screen reader", () => {
		const { mount } = fakeCanvas();
		render(
			graphView(
				data,
				route,
				new CanvasHost<WorkflowGraph, OnSelect>(
					mount,
					() => {},
					() => {},
				),
			),
			root,
		);
		const links = [...root.querySelectorAll<HTMLAnchorElement>(".gr-list a")];
		const hrefs = links.map((link) => link.getAttribute("href"));

		// Each task goes where a click on its node does: the shared panel.
		for (const row of tasks) {
			expect(hrefs).toContain(`#graph?slug=${row.slug}`);
		}
		// And each project to its rollup.
		expect(hrefs).toContain(`#projects?project=${projects[0]?.slug}`);
		expect(links).toHaveLength(tasks.length + projects.length);
	});

	it("says why a large graph is drawn collapsed", () => {
		const { mount } = fakeCanvas();
		const many = Array.from({ length: CLUSTER_ABOVE }, (_, index) =>
			task(`tk-many-${index}`, { project_slug: null }),
		);
		const host = new CanvasHost<WorkflowGraph, OnSelect>(
			mount,
			() => {},
			() => {},
		);

		render(
			graphView(
				{ ...data, tasks: many.slice(0, CLUSTER_ABOVE - 1) },
				route,
				host,
			),
			root,
		);
		expect(root.textContent).not.toContain("drawn as one node");

		render(graphView({ ...data, tasks: many }, route, host), root);
		expect(root.textContent).toContain("drawn as one node");
	});

	it("says there is nothing to draw rather than drawing an empty canvas", () => {
		const { mount } = fakeCanvas();
		render(
			graphView(
				{ projects: [], tasks: [], truncated: false },
				route,
				new CanvasHost<WorkflowGraph, OnSelect>(
					mount,
					() => {},
					() => {},
				),
			),
			root,
		);
		expect(root.textContent).toContain("No active projects or open tasks");
		expect(mount).not.toHaveBeenCalled();
	});

	it("says when the task read hit its limit", () => {
		const { mount } = fakeCanvas();
		render(
			graphView(
				{ ...data, truncated: true },
				route,
				new CanvasHost<WorkflowGraph, OnSelect>(
					mount,
					() => {},
					() => {},
				),
			),
			root,
		);
		expect(root.textContent).toContain("hit its limit");
	});
});

describe("CanvasHost", () => {
	it("destroys the network when its container goes", async () => {
		const { canvas, mount } = fakeCanvas();
		const host = new CanvasHost<WorkflowGraph, OnSelect>(
			mount,
			() => {},
			() => {},
		);
		host.attach(document.createElement("div"));
		await vi.waitFor(() => expect(mount).toHaveBeenCalled());
		await Promise.resolve();

		host.attach(undefined);

		expect(canvas.destroy).toHaveBeenCalledTimes(1);
	});

	it("says the drawing code failed to load rather than showing an empty box", async () => {
		// e.g. the server was upgraded under an open page and the old hashed
		// chunk now 404s.
		const mount = vi.fn(async () => {
			throw new Error("Failed to fetch dynamically imported module");
		});
		const onError = vi.fn();
		const host = new CanvasHost<WorkflowGraph, OnSelect>(
			mount,
			() => {},
			onError,
		);
		const root = document.createElement("div");
		const data: GraphData = { projects, tasks, truncated: false };

		render(graphView(data, route, host), root);
		await vi.waitFor(() => expect(onError).toHaveBeenCalled());
		render(graphView(data, route, host), root);

		expect(root.querySelector("[role=alert]")?.textContent).toContain(
			"Failed to fetch dynamically imported module",
		);
		expect(root.querySelector(".graph-canvas")).toBeNull();
	});

	it("drops a mount that lands after the container was replaced", async () => {
		// The import resolves after the tab was left and re-entered: the first
		// network would otherwise draw into a detached element and leak.
		const first = { update: vi.fn(), destroy: vi.fn() };
		const second = { update: vi.fn(), destroy: vi.fn() };
		let resolveFirst: (canvas: Canvas<WorkflowGraph>) => void = () => {};
		const mount = vi
			.fn<(element: HTMLElement) => Promise<Canvas<WorkflowGraph>>>()
			.mockImplementationOnce(
				() =>
					new Promise((resolve) => {
						resolveFirst = resolve;
					}),
			)
			.mockResolvedValueOnce(second);
		const host = new CanvasHost<WorkflowGraph, OnSelect>(
			mount,
			() => {},
			() => {},
		);
		host.show({ nodes: [], edges: [], clusters: [] }, null);

		host.attach(document.createElement("div"));
		host.attach(document.createElement("div"));
		await vi.waitFor(() => expect(second.update).toHaveBeenCalled());
		resolveFirst(first);
		await Promise.resolve();

		expect(first.destroy).toHaveBeenCalledTimes(1);
		expect(first.update).not.toHaveBeenCalled();
	});
});
