import { html, nothing, svg, type TemplateResult } from "lit-html";
import { ref } from "lit-html/directives/ref.js";
import { emptyBox, errorBox } from "../chrome.js";
import { repoLabel } from "../format.js";
import { type Route, routeHref } from "../route.js";
import type { TaskRow, WorkflowProjectSummary } from "../types.js";

/**
 * The Graph tab (spec §6.8): `witan graph`'s project and task graph, drawn
 * with the bundled vis-network.
 *
 * `scopeTasks` and `buildGraph` are ports of `witan/visualize.py`'s
 * `scope_tasks` and `build_graph`. The duplication is a presentation
 * transform over the same two reads the CLI makes, not a second read surface,
 * which is why spec §6.8 allows it. `graph.test.ts` holds the port to the
 * Python by comparing against `fixtures/graph.json`, which
 * `bin/gen_ui_fixtures.py` records by running the real transform over the
 * recorded `task_list` and `workflow_project_list` results.
 *
 * vis-network itself lives in `graph-canvas.ts` and is loaded on first use,
 * so the other tabs do not pay for it.
 */

/** Node fills by status, as `visualize.py` has them. */
export const PROJECT_COLORS: Record<string, string> = {
	active: "#56b870",
	completed: "#4c9be8",
	abandoned: "#666",
};
export const TASK_COLORS: Record<string, string> = {
	open: "#e8a33d",
	in_progress: "#6fc8e8",
	blocked: "#e85454",
	closed: "#555",
};
/**
 * Edge colours. `belongs_to` is `null` because the CLI's `#444` disappears
 * on a dark background: the canvas uses the theme's muted colour for it.
 */
export const EDGE_COLORS: Record<EdgeKind, string | null> = {
	belongs_to: null,
	parent: "#9b7be8",
	blocks: "#e85454",
};

export type EdgeKind = "belongs_to" | "parent" | "blocks";

export interface GraphNode {
	id: string;
	label: string;
	group: "project" | "task";
	color: string;
	status: string;
	/** Plain text. vis-network renders a string title as text, never as HTML. */
	tooltip: string;
	/** The untruncated title, for the list view. */
	title: string;
	/** The `GraphCluster.key` this node collapses into on a large graph. */
	cluster: string;
}

/**
 * A project and its tasks, or the tasks of one repo that have no project in
 * the graph. What a large graph collapses into (see `CLUSTER_ABOVE`), and
 * what the list view groups by.
 */
export interface GraphCluster {
	/** The project's slug, or `repo:<uri>` for a repo's projectless tasks. */
	key: string;
	label: string;
	/** The project's slug, or `null` for a repo group. */
	project: string | null;
	color: string;
	/** Task slugs, in graph order. */
	tasks: string[];
}

export interface GraphEdge {
	src: string;
	dst: string;
	kind: EdgeKind;
	label: string;
}

export interface WorkflowGraph {
	nodes: GraphNode[];
	edges: GraphEdge[];
	clusters: GraphCluster[];
}

/**
 * Above this many nodes the canvas collapses each cluster into one node.
 *
 * Measured with the real canvas in headless Chromium: 400 tasks lay out in
 * about a quarter of a second of main-thread time, 800 take 38 seconds, and
 * 1,200 were still laying out after a minute. A graph-wide scope was about
 * 1,150 live tasks when this was written.
 */
export const CLUSTER_ABOVE = 400;

/** What the Graph tab reads, as the app hands it over. */
export interface GraphData {
	projects: WorkflowProjectSummary[];
	tasks: TaskRow[];
	/** The task read came back at its limit, so there may be more. */
	truncated: boolean;
}

/** `scope_tasks`: drop closed tasks unless asked, and tasks of unlisted projects. */
export function scopeTasks(
	projects: WorkflowProjectSummary[],
	tasks: TaskRow[],
	includeClosed = false,
): TaskRow[] {
	const listed = new Set(projects.map((project) => project.slug));
	return tasks.filter(
		(task) =>
			(includeClosed || task.status !== "closed") &&
			(!task.project_slug || listed.has(task.project_slug)),
	);
}

/**
 * `build_graph`, node for node and edge for edge, in the same order.
 *
 * The labels are truncated at the same lengths the CLI uses (40 for a
 * project, 35 for a task) and a p0 or p1 task carries its priority, so a node
 * reads the same in either renderer.
 */
export function buildGraph(
	projects: WorkflowProjectSummary[],
	tasks: TaskRow[],
): WorkflowGraph {
	const nodes: GraphNode[] = [];
	const edges: GraphEdge[] = [];
	const projectSlugs = new Set<string>();
	const taskSlugs = new Set<string>();
	const clusters = new Map<string, GraphCluster>();

	for (const project of projects) {
		projectSlugs.add(project.slug);
		const status = project.status || "active";
		const color = PROJECT_COLORS[status] ?? "#56b870";
		clusters.set(project.slug, {
			key: project.slug,
			label: project.title || project.slug,
			project: project.slug,
			color,
			tasks: [],
		});
		nodes.push({
			id: project.slug,
			label: truncate(project.title || project.slug, 40),
			group: "project",
			color,
			status,
			tooltip: `${project.slug}\nphase: ${project.phase ?? "?"} · status: ${status}`,
			title: project.title || project.slug,
			cluster: project.slug,
		});
	}

	for (const task of tasks) {
		taskSlugs.add(task.slug);
		const status = task.status || "open";
		const priority = task.priority || "p2";
		const base = truncate(task.title || task.slug, 35);
		const key =
			task.project_slug && projectSlugs.has(task.project_slug)
				? task.project_slug
				: `repo:${task.repo ?? ""}`;
		let cluster = clusters.get(key);
		if (!cluster) {
			cluster = {
				key,
				label: `${task.repo ? repoLabel(task.repo) : "No repo"} (no project)`,
				project: null,
				color: PROJECT_COLORS.abandoned ?? "",
				tasks: [],
			};
			clusters.set(key, cluster);
		}
		cluster.tasks.push(task.slug);
		nodes.push({
			id: task.slug,
			label:
				priority === "p0" || priority === "p1" ? `[${priority}] ${base}` : base,
			group: "task",
			color: TASK_COLORS[status] ?? "#e8a33d",
			status,
			tooltip: `${task.slug}\nstatus: ${status} · priority: ${priority}\n${task.title}`,
			title: task.title || task.slug,
			cluster: key,
		});
	}

	for (const task of tasks) {
		if (task.project_slug && projectSlugs.has(task.project_slug)) {
			edges.push({
				src: task.slug,
				dst: task.project_slug,
				kind: "belongs_to",
				label: "",
			});
		}
		if (task.parent_slug && taskSlugs.has(task.parent_slug)) {
			edges.push({
				src: task.slug,
				dst: task.parent_slug,
				kind: "parent",
				label: "child of",
			});
		}
		for (const blocker of task.blocked_by ?? []) {
			if (taskSlugs.has(blocker)) {
				edges.push({
					src: blocker,
					dst: task.slug,
					kind: "blocks",
					label: "blocks",
				});
			}
		}
	}

	return { nodes, edges, clusters: [...clusters.values()] };
}

/**
 * The first `length` code points of `text`, as Python's `text[:length]`.
 *
 * `String.slice` counts UTF-16 code units, so it cuts an emoji at the
 * boundary in half and draws the orphaned surrogate as a replacement
 * character, where the CLI keeps or drops the whole character.
 */
function truncate(text: string, length: number): string {
	return Array.from(text).slice(0, length).join("");
}

/** What the canvas module hands back once vis-network is mounted. */
export interface Canvas {
	update(graph: WorkflowGraph, selected: string | null): void;
	destroy(): void;
}

/** Called with the clicked node and which kind of node it is. */
export type OnSelect = (id: string, group: GraphNode["group"]) => void;

export type MountCanvas = (
	element: HTMLElement,
	onSelect: OnSelect,
) => Promise<Canvas>;

/**
 * Keeps one vis-network alive across renders.
 *
 * lit-html re-renders the whole app on every poll, and a network rebuilt each
 * time would re-run its physics and scatter the layout every 30 seconds. So
 * the network is mounted once per container element and fed the new graph,
 * and only a new element (the tab left and came back) mounts a new one.
 *
 * `mount` is injected so the app's tests can run without a canvas: jsdom has
 * none, and vis-network draws on nothing else.
 */
export class CanvasHost {
	private element: Element | null = null;
	private canvas: Canvas | null = null;
	private pending: { graph: WorkflowGraph; selected: string | null } | null =
		null;
	/** Bumped per mount, so a mount that lands after a newer one is dropped. */
	private generation = 0;
	/**
	 * Why the network could not be mounted, e.g. the lazily imported chunk
	 * 404ing because the server was upgraded under an open page. Kept until a
	 * reload: the chunk name is baked into this page's bundle, so retrying the
	 * same import cannot succeed.
	 */
	error: Error | null = null;

	constructor(
		private readonly mount: MountCanvas,
		private readonly onSelect: OnSelect,
		private readonly onError: () => void,
	) {}

	/** The `ref` callback: called with the container, or `undefined` on removal. */
	readonly attach = (element: Element | undefined): void => {
		if (element === this.element) {
			return;
		}
		this.release();
		if (!(element instanceof HTMLElement)) {
			return;
		}
		this.element = element;
		const generation = this.generation;
		this.mount(element, this.onSelect).then(
			(canvas) => {
				if (generation !== this.generation) {
					canvas.destroy();
					return;
				}
				this.canvas = canvas;
				if (this.pending) {
					canvas.update(this.pending.graph, this.pending.selected);
				}
			},
			(error: unknown) => {
				if (generation !== this.generation) {
					return;
				}
				this.error = error instanceof Error ? error : new Error(String(error));
				this.onError();
			},
		);
	};

	show(graph: WorkflowGraph, selected: string | null): void {
		this.pending = { graph, selected };
		this.canvas?.update(graph, selected);
	}

	/** Drop the network, e.g. when the tab is left. */
	release(): void {
		this.generation += 1;
		this.canvas?.destroy();
		this.canvas = null;
		this.element = null;
	}
}

export function graphView(
	data: GraphData,
	route: Route,
	host: CanvasHost,
): TemplateResult {
	const graph = buildGraph(
		data.projects,
		scopeTasks(data.projects, data.tasks, route.closed),
	);
	if (graph.nodes.length === 0) {
		host.release();
		return emptyBox(
			route.closed
				? "No projects or tasks to draw."
				: "No active projects or open tasks to draw.",
		);
	}
	if (host.error) {
		return errorBox(
			new Error(`${host.error.message} Reload the page to try again.`),
			"the graph drawing code",
		);
	}
	host.show(graph, route.slug);
	const projects = graph.nodes.filter((node) => node.group === "project");
	const tasks = graph.nodes.length - projects.length;
	return html`
    <section class="graph" aria-label="Graph">
      <p class="note">
        ${projects.length} ${projects.length === 1 ? "project" : "projects"} ·
        ${tasks} ${tasks === 1 ? "task" : "tasks"} · ${graph.edges.length}
        ${graph.edges.length === 1 ? "edge" : "edges"}. Click a task for its
        detail, or a project for its rollup.
      </p>
      ${
				graph.nodes.length > CLUSTER_ABOVE
					? html`<p class="note">
              That is more than lays out usefully at once, so each project, and
              each repo's tasks with no project, is drawn as one node. Click one
              to open it, or narrow the graph by repo or project.
            </p>`
					: nothing
			}
      ${
				data.truncated
					? html`<p class="note">
              The task read hit its limit, so the graph may be missing tasks.
            </p>`
					: nothing
			}
      ${legend()}
      <div
        class="graph-canvas"
        role="img"
        aria-label="Project and task graph. The List view below has every node as a link."
        ${ref(host.attach)}
      ></div>
      ${listView(graph, route)}
    </section>
  `;
}

/**
 * Every node as a link, grouped as the canvas clusters them.
 *
 * The canvas is pointer-only: its nodes are neither focusable nor in the
 * accessibility tree. This is the same graph for a keyboard or a screen
 * reader, and its links go where a click on the canvas does.
 */
function listView(graph: WorkflowGraph, route: Route): TemplateResult {
	const byId = new Map(graph.nodes.map((node) => [node.id, node]));
	return html`
    <details class="gr-list">
      <summary>List view</summary>
      <ul>
        ${graph.clusters.map(
					(cluster) => html`<li>
            ${
							cluster.project
								? html`<a
                    href=${routeHref(route, {
											view: "projects",
											project: cluster.project,
											slug: null,
										})}
                    >${cluster.label}</a
                  >`
								: cluster.label
						}
            ${
							cluster.tasks.length > 0
								? html`<ul>
                    ${cluster.tasks.map((slug) => {
											const node = byId.get(slug);
											return html`<li>
                        <a href=${routeHref(route, { slug })}
                          >${node?.title ?? slug}</a
                        >
                        <span class="muted">${node?.status}</span>
                      </li>`;
										})}
                  </ul>`
								: nothing
						}
          </li>`,
				)}
      </ul>
    </details>
  `;
}

const LEGEND: { label: string; color: string; project: boolean }[] = [
	{
		label: "project (active)",
		color: PROJECT_COLORS.active ?? "",
		project: true,
	},
	{
		label: "project (done)",
		color: PROJECT_COLORS.completed ?? "",
		project: true,
	},
	{
		label: "project (abandoned)",
		color: PROJECT_COLORS.abandoned ?? "",
		project: true,
	},
	{ label: "open", color: TASK_COLORS.open ?? "", project: false },
	{
		label: "in progress",
		color: TASK_COLORS.in_progress ?? "",
		project: false,
	},
	{ label: "blocked", color: TASK_COLORS.blocked ?? "", project: false },
	{ label: "closed", color: TASK_COLORS.closed ?? "", project: false },
];

/**
 * The key, as SVG swatches.
 *
 * ★ SVG `fill`, not a `style=` background: `/ui/`'s CSP blocks inline style
 * attributes, and a presentation attribute is not one.
 */
function legend(): TemplateResult {
	return html`
    <ul class="gr-legend" aria-label="Key">
      ${LEGEND.map(
				(entry) => html`<li>
          <svg class="gr-swatch" viewBox="0 0 10 10" aria-hidden="true">
            ${
							entry.project
								? svg`<polygon points="5,0 10,5 5,10 0,5" fill=${entry.color}></polygon>`
								: svg`<circle cx="5" cy="5" r="4" fill=${entry.color}></circle>`
						}
          </svg>
          ${entry.label}
        </li>`,
			)}
      <li>
        <svg class="gr-swatch" viewBox="0 0 10 10" aria-hidden="true">
          <line x1="0" y1="5" x2="10" y2="5" stroke=${EDGE_COLORS.blocks ?? ""} stroke-width="2"></line>
        </svg>
        blocks
      </li>
      <li>
        <svg class="gr-swatch" viewBox="0 0 10 10" aria-hidden="true">
          <line x1="0" y1="5" x2="10" y2="5" stroke=${EDGE_COLORS.parent ?? ""} stroke-width="2"></line>
        </svg>
        child of
      </li>
    </ul>
  `;
}
