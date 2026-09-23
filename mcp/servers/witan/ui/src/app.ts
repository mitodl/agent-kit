import { html, nothing, render, type TemplateResult } from "lit-html";
import { emptyBox, placeholderFor, readStatus } from "./chrome.js";
import { DAY } from "./format.js";
import { KeyedRead, LiveRead, type Snapshot } from "./live.js";
import {
	codeGraphAvailable,
	codeInterfaceConsumers,
	codeInterfaceProviders,
	codeRepoDependencies,
	memoryContradictions,
	memoryGet,
	memoryList,
	memoryNeighbors,
	memorySearch,
	recall,
	taskGet,
	taskList,
	taskReady,
	topicGet,
	workflowProjectGet,
	workflowProjectList,
	workflowProjectStatus,
	workflowSessionList,
} from "./mcp.js";
import { formatRoute, parseRoute, type Route } from "./route.js";
import { detailPanel, shell } from "./shell.js";
import type {
	InterfaceBinding,
	RepoDependencies,
	TaskDetail,
	WorkflowProjectSummary,
} from "./types.js";
import { type Board, board, TASK_LIMIT } from "./views/board.js";
import {
	type BridgeGraph,
	bindingTable,
	bridgeView,
	drillPlan,
	type OnSelectEdge,
	parseContractKey,
	parseEdgeKey,
	producingRows,
} from "./views/bridge.js";
import { CanvasHost } from "./views/canvas-host.js";
import {
	type GraphData,
	graphView,
	type OnSelect,
	type WorkflowGraph,
} from "./views/graph.js";
import {
	isMemorySlug,
	type MemoryPage,
	type MemoryPanel,
	memoryDetail,
	memoryMissing,
	memoryView,
} from "./views/memory.js";
import { projectList, projectRollup, type Rollup } from "./views/projects.js";
import { taskDetail, taskMissing } from "./views/task-detail.js";
import { type Timeline, timeline } from "./views/timeline.js";
import { type Waves, waves, wavesPicker } from "./views/waves.js";

/**
 * The wiring: route in, reads out, one render.
 *
 * Every other module in the app is a pure function or a class with no opinion
 * about the DOM. This is the one place that owns mutable state, and it holds
 * exactly two kinds of thing: the route, and one read per thing the current
 * route draws.
 */

export class App {
	private route: Route;
	private readonly root: HTMLElement;

	/**
	 * The project list, always read across ALL repos.
	 *
	 * ★ NOT SCOPED TO `route.repo`, and that is deliberate. The repo filter's
	 * options are derived from what comes back, so a read already narrowed to
	 * one repo would leave the filter showing only the repo you are in and no
	 * way back to the others. The narrowing happens in the browser instead,
	 * over a list that is small by construction.
	 */
	private readonly projects: LiveRead<WorkflowProjectSummary[]>;
	private projectsSnapshot: Snapshot<WorkflowProjectSummary[]>;

	private readonly rollup = new KeyedRead<Rollup>(() => this.draw());
	private readonly board = new KeyedRead<Board>(() => this.draw());
	/**
	 * No interval (spec §6.6): it plots elapsed time, so a poll every 30
	 * seconds would only move the right edge. Focus and Refresh still read.
	 */
	private readonly timeline = new KeyedRead<Timeline>(() => this.draw(), {
		intervalMs: 0,
	});
	private readonly waves = new KeyedRead<Waves>(() => this.draw());
	private readonly memoryPage = new KeyedRead<MemoryPage>(() => this.draw());
	private readonly graph = new KeyedRead<GraphData>(() => this.draw());
	/**
	 * No interval, as the timeline has none: the code graph changes when a
	 * repo is reindexed, not every 30 seconds, and this read walks every
	 * binding in the bridge store. Focus and Refresh still read.
	 */
	private readonly bridge = new KeyedRead<RepoDependencies>(() => this.draw(), {
		intervalMs: 0,
	});
	/** One contract's bindings on the open Bridge edge. */
	private readonly drill = new KeyedRead<InterfaceBinding[]>(
		() => this.draw(),
		{ intervalMs: 0 },
	);
	/**
	 * Whether the server has witan-code's tools, from the `tools/list` the
	 * client reads on connect. `null` until that answers, so the Bridge tab
	 * appears once the server says it can back it, never before.
	 */
	private codeGraph: boolean | null = null;
	private probing = false;
	private readonly bridgeCanvas = new CanvasHost<BridgeGraph, OnSelectEdge>(
		(element, onSelect) =>
			import("./views/bridge-canvas.js").then((module) =>
				module.mountBridgeCanvas(element, onSelect),
			),
		(edge) => this.navigate({ edge, binding: null }),
		() => this.draw(),
	);
	/**
	 * vis-network is imported on first use, so the other tabs never load it.
	 * A task opens in the shared panel; a project, which the panel does not
	 * draw, opens its rollup.
	 */
	private readonly canvas = new CanvasHost<WorkflowGraph, OnSelect>(
		(element, onSelect) =>
			import("./views/graph-canvas.js").then((module) =>
				module.mountCanvas(element, onSelect),
			),
		(slug, group) =>
			this.navigate(
				group === "project"
					? { view: "projects", project: slug, slug: null }
					: { slug },
			),
		() => this.draw(),
	);
	private readonly detail = new KeyedRead<TaskDetail | null>(() => this.draw());
	private readonly memoryDetail = new KeyedRead<MemoryPanel>(() => this.draw());

	/** The slug the panel last rendered, so focus moves only when it opens. */
	private focusedSlug: string | null = null;

	/** What had focus when the panel opened, to hand it back on close. */
	private returnFocusTo: HTMLElement | null = null;

	constructor(root: HTMLElement, hash: string = window.location.hash) {
		this.root = root;
		this.route = parseRoute(hash);
		this.projects = new LiveRead(
			() => workflowProjectList({ repo: "" }),
			(snapshot) => {
				this.projectsSnapshot = snapshot;
				if (snapshot.error === null && snapshot.loadedAt !== null) {
					this.probeCodeGraph();
				}
				this.draw();
			},
		);
		this.projectsSnapshot = this.projects.snapshot;
	}

	start(): void {
		window.addEventListener("hashchange", this.onHashChange);
		document.addEventListener("keydown", this.onKeyDown);
		this.projects.start();
		this.syncReads();
		this.draw();
		this.probeCodeGraph();
	}

	/**
	 * Ask whether the server has the code graph, until it answers.
	 *
	 * A failure is a failure to connect, which every read on the page already
	 * reports, so it is not reported again here. It is retried instead, from
	 * the project list's next successful read: without that, a page loaded
	 * while the server restarted would hide the Bridge tab for its lifetime
	 * while every other tab recovered.
	 */
	private probeCodeGraph(): void {
		if (this.codeGraph !== null || this.probing) {
			return;
		}
		this.probing = true;
		codeGraphAvailable()
			.then(
				(available) => {
					this.codeGraph = available;
					this.syncReads();
				},
				() => {},
			)
			.finally(() => {
				this.probing = false;
				this.draw();
			});
	}

	stop(): void {
		window.removeEventListener("hashchange", this.onHashChange);
		document.removeEventListener("keydown", this.onKeyDown);
		this.projects.stop();
		this.rollup.stop();
		this.board.stop();
		this.timeline.stop();
		this.waves.stop();
		this.memoryPage.stop();
		this.graph.stop();
		this.canvas.release();
		this.bridge.stop();
		this.drill.stop();
		this.bridgeCanvas.release();
		this.detail.stop();
		this.memoryDetail.stop();
	}

	private readonly onHashChange = (): void => {
		this.route = parseRoute(window.location.hash);
		this.syncReads();
		this.draw();
	};

	/**
	 * Escape closes the panel.
	 *
	 * Through the route rather than by hiding a node, so the address bar, the
	 * back button and the close link all agree about what is open.
	 */
	private readonly onKeyDown = (event: KeyboardEvent): void => {
		if (event.key === "Escape" && this.route.slug) {
			this.navigate({ slug: null });
		}
	};

	/** Follow a route patch. Goes through the URL so every navigation is linkable. */
	private navigate(patch: Partial<Route>): void {
		const next = formatRoute({ ...this.route, ...patch });
		if (next === window.location.hash) {
			return;
		}
		window.location.hash = next;
	}

	/**
	 * Point the per-route reads at what the current route needs.
	 *
	 * Each key is that read's arguments and nothing else the route carries (see
	 * `KeyedRead`), so opening a panel does not re-read the view beside it.
	 */
	private syncReads(): void {
		const route = this.route;

		// Only the Projects tab draws a rollup. Keyed on the project alone, a
		// route like `#board?project=wp-x` kept four tool calls going every 30
		// seconds behind a tab that does not draw it. The project slug is the
		// WHOLE key: `repo` is not an argument to any of the four reads (see
		// `readRollup`), so keying on it only bought a pointless re-read.
		const project = route.view === "projects" ? route.project : null;
		this.rollup.sync(project, () => readRollup(project as string));

		// Every argument any of the board's reads takes, and nothing else, so
		// opening a card in the panel does not re-read five tools.
		const boardScope =
			route.view === "board"
				? { repo: route.repo, project: route.project, closed: route.closed }
				: null;
		this.board.sync(boardScope && JSON.stringify(boardScope), () =>
			readBoard(boardScope as BoardScope),
		);

		// The repo is not in the key: both reads are repo-wide (see
		// `readTimeline`) and the repo narrows them in the browser.
		const timelineScope =
			route.view === "timeline"
				? { project: route.project, days: route.days }
				: null;
		this.timeline.sync(timelineScope && JSON.stringify(timelineScope), () =>
			readTimeline(timelineScope as TimelineScope),
		);

		// Waves are per project (spec §6.5), so without one there is nothing to
		// read: the view offers the shell's project list to pick from instead.
		const wavesProject = route.view === "waves" ? route.project : null;
		this.waves.sync(wavesProject, () => readWaves(wavesProject as string));

		const memoryScope: MemoryScope | null =
			route.view === "memory"
				? {
						repo: route.repo,
						q: route.q,
						kind: route.kind,
						plain: route.plain,
						superseded: route.superseded,
						topic: route.topic,
					}
				: null;
		this.memoryPage.sync(memoryScope && JSON.stringify(memoryScope), () =>
			readMemoryPage(memoryScope as MemoryScope),
		);

		// Every argument either read takes, as the board's scope is.
		const graphScope =
			route.view === "graph"
				? { repo: route.repo, project: route.project }
				: null;
		this.graph.sync(graphScope && JSON.stringify(graphScope), () =>
			readGraph(graphScope as GraphScope),
		);

		// Only the tool arguments. The confidence floor and the generic toggle
		// filter in the browser, so changing them does not re-read.
		const bridgeOn = route.view === "bridge" && this.codeGraph === true;
		const bridgeScope = bridgeOn
			? { repo: route.repo, kind: route.contract, precise: route.precise }
			: null;
		this.bridge.sync(bridgeScope && JSON.stringify(bridgeScope), () =>
			readBridge(bridgeScope as BridgeScope),
		);
		const drillScope =
			bridgeOn && route.edge && route.binding
				? { edge: route.edge, binding: route.binding, floor: route.confidence }
				: null;
		this.drill.sync(drillScope && JSON.stringify(drillScope), () =>
			readDrill(drillScope as DrillScope),
		);

		// One panel, two kinds of slug: a memory linked from anywhere opens as a
		// memory, and everything else as a task.
		const slug = route.slug;
		const memorySlug = slug && isMemorySlug(slug) ? slug : null;
		const taskSlug = slug && !memorySlug ? slug : null;
		this.detail.sync(taskSlug, () => taskGet(taskSlug as string));
		this.memoryDetail.sync(memorySlug, () =>
			readMemoryPanel(memorySlug as string),
		);
	}

	/** The reads behind whatever the main area is showing, for the status line. */
	private primary(): { snapshot: Snapshot<unknown>; refresh: () => void } {
		for (const read of [
			this.board,
			this.timeline,
			this.waves,
			this.graph,
			this.bridge,
			this.rollup,
			this.memoryPage,
		] as KeyedRead<unknown>[]) {
			if (read.snapshot) {
				return { snapshot: read.snapshot, refresh: () => read.refresh() };
			}
		}
		return {
			snapshot: this.projectsSnapshot,
			refresh: () => this.projects.refresh(),
		};
	}

	private draw(): void {
		const { snapshot, refresh } = this.primary();
		const all = this.projectsSnapshot.data ?? [];
		const inScope = this.route.repo
			? all.filter((project) => (project.repos ?? []).includes(this.route.repo))
			: all;

		render(
			shell({
				route: this.route,
				repos: reposIn(all),
				projects: inScope.map((project) => ({
					value: project.slug,
					label: project.title,
				})),
				status: readStatus(snapshot, refresh),
				body: this.body(inScope),
				panel: this.panel(),
				onNavigate: (patch) => this.navigate(patch),
				codeGraph: this.codeGraph === true,
			}),
			this.root,
		);

		this.moveFocus();
	}

	private body(inScope: WorkflowProjectSummary[]): TemplateResult {
		if (this.route.view === "board") {
			const snapshot = this.board.snapshot;
			if (!snapshot) {
				return emptyBox("Reading the board…");
			}
			const waiting = placeholderFor(snapshot, "the board");
			if (waiting) {
				return waiting;
			}
			return board(snapshot.data as Board, this.route);
		}

		if (this.route.view === "timeline") {
			const snapshot = this.timeline.snapshot;
			if (!snapshot) {
				return emptyBox("Reading the timeline…");
			}
			const waiting = placeholderFor(snapshot, "the timeline");
			if (waiting) {
				return waiting;
			}
			return timeline(snapshot.data as Timeline, this.route);
		}

		if (this.route.view === "waves") {
			if (!this.route.project) {
				const waiting = placeholderFor(this.projectsSnapshot, "projects");
				return waiting ?? wavesPicker(inScope, this.route);
			}
			const snapshot = this.waves.snapshot;
			if (!snapshot) {
				return emptyBox("Reading the waves…");
			}
			const waiting = placeholderFor(snapshot, "the waves");
			if (waiting) {
				return waiting;
			}
			return waves(snapshot.data as Waves, this.route);
		}

		if (this.route.view === "memory") {
			const snapshot = this.memoryPage.snapshot;
			if (!snapshot) {
				return emptyBox("Reading memories…");
			}
			const waiting = placeholderFor(snapshot, "memories");
			if (waiting) {
				return waiting;
			}
			// `readMemoryPage` always resolves to an object.
			return memoryView(snapshot.data as MemoryPage, this.route, (patch) =>
				this.navigate(patch),
			);
		}

		if (this.route.view === "bridge") {
			if (this.codeGraph === null) {
				return emptyBox("Checking whether this server has the code graph…");
			}
			if (!this.codeGraph) {
				// A link pasted from a server that has witan-code, opened on one
				// that does not.
				return emptyBox(
					"This server has no code graph: witan-code is not installed alongside it.",
				);
			}
			const snapshot = this.bridge.snapshot;
			if (!snapshot) {
				return emptyBox("Reading the code graph…");
			}
			const waiting = placeholderFor(snapshot, "the code graph");
			if (waiting) {
				return waiting;
			}
			return bridgeView({
				deps: snapshot.data as RepoDependencies,
				route: this.route,
				host: this.bridgeCanvas,
				drill: this.drillSection(),
				onNavigate: (patch) => this.navigate(patch),
			});
		}

		if (this.route.view === "graph") {
			const snapshot = this.graph.snapshot;
			if (!snapshot) {
				return emptyBox("Reading the graph…");
			}
			const waiting = placeholderFor(snapshot, "the graph");
			if (waiting) {
				return waiting;
			}
			return graphView(snapshot.data as GraphData, this.route, this.canvas);
		}

		if (this.route.project) {
			const snapshot = this.rollup.snapshot;
			if (!snapshot) {
				return emptyBox("Reading project…");
			}
			const waiting = placeholderFor(snapshot, "this project");
			if (waiting) {
				return waiting;
			}
			// `placeholderFor` returns null only once a read has landed, and
			// `readRollup` always resolves to an object.
			return projectRollup(snapshot.data as Rollup, this.route);
		}

		const waiting = placeholderFor(this.projectsSnapshot, "projects");
		if (waiting) {
			return waiting;
		}
		return projectList(inScope, this.route);
	}

	/** The open contract's bindings, in whatever state their read is in. */
	private drillSection(): TemplateResult | typeof nothing {
		const snapshot = this.drill.snapshot;
		if (!snapshot) {
			return nothing;
		}
		return (
			placeholderFor(snapshot, "the bindings") ??
			bindingTable(snapshot.data as InterfaceBinding[])
		);
	}

	private panel(): TemplateResult | typeof nothing {
		const slug = this.route.slug;
		if (!slug) {
			return nothing;
		}
		if (isMemorySlug(slug)) {
			return this.memoryPanel(slug);
		}
		const snapshot = this.detail.snapshot;
		if (!snapshot) {
			return detailPanel(
				this.route,
				slug,
				html`<p class="placeholder" aria-busy="true">Reading ${slug}…</p>`,
			);
		}
		// ★ `placeholderFor` keys on whether a read has LANDED, not on the data.
		// `task_get` returns `null` as a value for a slug the graph does not
		// have, so testing the data would leave every stale link reading
		// "Reading…" forever.
		const waiting = placeholderFor(snapshot, slug);
		if (waiting) {
			return detailPanel(this.route, slug, waiting);
		}
		// The panel carries its own read state: it polls separately from the
		// view underneath, so the top bar cannot report for it.
		const status = readStatus(snapshot, () => this.detail.refresh());
		const task = snapshot.data;
		// A null with a result behind it is the graph saying no: a stale link,
		// which is normal, rather than a failure.
		if (task === null) {
			return detailPanel(this.route, slug, taskMissing(slug), status);
		}
		return detailPanel(
			this.route,
			task.title,
			taskDetail(task, this.route),
			status,
		);
	}

	/** The panel for a memory slug. The same states as the task panel's. */
	private memoryPanel(slug: string): TemplateResult {
		const snapshot = this.memoryDetail.snapshot;
		if (!snapshot) {
			return detailPanel(
				this.route,
				slug,
				html`<p class="placeholder" aria-busy="true">Reading ${slug}…</p>`,
			);
		}
		const waiting = placeholderFor(snapshot, slug);
		if (waiting) {
			return detailPanel(this.route, slug, waiting);
		}
		const status = readStatus(snapshot, () => this.memoryDetail.refresh());
		// `readMemoryPanel` always resolves to an object once a read has landed.
		const panel = snapshot.data as MemoryPanel;
		if (panel.memory === null) {
			return detailPanel(this.route, slug, memoryMissing(slug), status);
		}
		return detailPanel(
			this.route,
			panel.memory.title,
			memoryDetail({ ...panel, memory: panel.memory }, this.route),
			status,
		);
	}

	/**
	 * Move focus into the panel when it opens, and back where it came from when
	 * it closes.
	 *
	 * Only on a change of slug: re-focusing on every poll would steal the caret
	 * out of whatever a person was reading every 30 seconds.
	 *
	 * Returning focus matters because the panel leaves the DOM when it closes,
	 * and a keyboard user whose focus was inside it lands back on `<body>` —
	 * which means tabbing again starts at the top of the page rather than at
	 * the link they opened the task from.
	 */
	private moveFocus(): void {
		if (this.route.slug === this.focusedSlug) {
			return;
		}
		const opening = this.route.slug !== null && this.focusedSlug === null;
		const closing = this.route.slug === null && this.focusedSlug !== null;
		this.focusedSlug = this.route.slug;

		if (opening) {
			// Only what is focusable is worth returning to; `document.body` is the
			// resting state and returning to it is what already happens.
			const active = document.activeElement;
			this.returnFocusTo =
				active instanceof HTMLElement && active !== document.body
					? active
					: null;
		}
		if (this.route.slug) {
			this.root.querySelector<HTMLElement>(".detail-panel")?.focus();
			return;
		}
		if (closing && this.returnFocusTo?.isConnected) {
			this.returnFocusTo.focus();
		}
		this.returnFocusTo = null;
	}
}

/**
 * The four reads behind one project's rollup (spec §6.2).
 *
 * In parallel and all-or-nothing: they describe one project at one moment, and
 * a rollup drawn from three of four would show a task list that disagrees with
 * the counts beside it.
 */
async function readRollup(slug: string): Promise<Rollup> {
	const [detail, status, tasks, sessions] = await Promise.all([
		workflowProjectGet(slug),
		workflowProjectStatus(slug),
		// ★ NO `repo`, AND NO `limit`. `task_list` returns early on
		// `project_slug` (server.py: `if project_slug:`), before `repo` is ever
		// read, so passing one is a no-op that only looks like a filter. And the
		// by-project query is uncapped, so `limit` cannot widen this read — it
		// can only silently drop tasks past the cap, which would contradict both
		// the rollup's own promise and `workflow_project_status`'s counts.
		taskList({ repo: "", project_slug: slug }),
		workflowSessionList({ project_slug: slug }),
	]);
	return { detail, status, tasks, sessions };
}

/** The route fields that are arguments to the board's reads. */
type BoardScope = Pick<Route, "repo" | "project" | "closed">;

/**
 * The reads behind the board (spec §6.4), all-or-nothing like the rollup's.
 *
 * Ready is read with the route's scope, because it IS `task_ready` for that
 * scope. The non-closed statuses are read across every repo instead and
 * narrowed in the browser (`views/board.ts` `inScope`), because a blocked card
 * has to say whether each blocker is still open and blockers cross repos.
 * Three status reads rather than one unfiltered one, so the live set never
 * drags every closed task in the graph along with it.
 */
async function readBoard(scope: BoardScope): Promise<Board> {
	// `project_slug` overrides `repo` in both tools, so a project scope passes
	// `repo: ""` rather than a repo the server would ignore anyway.
	const scoped = scope.project
		? { repo: "", project_slug: scope.project }
		: { repo: scope.repo };
	const everywhere = (status: string) =>
		taskList({ repo: "", status, limit: TASK_LIMIT });
	const [ready, open, blocked, inProgress, closed] = await Promise.all([
		taskReady({ ...scoped, limit: TASK_LIMIT }),
		everywhere("open"),
		everywhere("blocked"),
		everywhere("in_progress"),
		scope.closed
			? taskList({ ...scoped, status: "closed", limit: TASK_LIMIT })
			: Promise.resolve(null),
	]);
	return {
		ready,
		live: [...open, ...blocked, ...inProgress],
		closed,
		truncated: [ready, open, blocked, inProgress, closed ?? []].some(
			(rows) => rows.length >= TASK_LIMIT,
		),
	};
}

/** The route fields that are arguments to the timeline's reads. */
type TimelineScope = Pick<Route, "project" | "days">;

/**
 * The reads behind the timeline (spec §6.6), all-or-nothing like the others.
 *
 * Tasks are read whole and windowed in the browser: `task_list` has no time
 * filter, and a task created long before the window can still be open or have
 * closed inside it. Sessions are windowed by the server through `since`, which
 * trims what crosses the wire but not what the server reads: it filters in
 * Python after reading every session (`read.gq` has no DateTime comparison,
 * spec §3.7), and keeps every session that never ended, however old. Across
 * every repo, for the reason the board's live read is.
 */
async function readTimeline(scope: TimelineScope): Promise<Timeline> {
	const readAt = Date.now();
	const since = new Date(readAt - scope.days * DAY).toISOString();
	const [tasks, sessions, projects] = await Promise.all([
		// `project_slug` returns early and uncapped; see `readRollup` for why a
		// limit there would only drop rows.
		scope.project
			? taskList({ repo: "", project_slug: scope.project })
			: taskList({ repo: "", limit: TASK_LIMIT }),
		workflowSessionList(
			scope.project ? { since, project_slug: scope.project } : { since },
		),
		// `status: null` is every status; omitted, the tool lists active ones.
		workflowProjectList({ repo: "", status: null }),
	]);
	return {
		tasks,
		sessions,
		readAt,
		days: scope.days,
		truncated: !scope.project && tasks.length >= TASK_LIMIT,
		projects,
	};
}

/**
 * The reads behind one project's waves (spec §6.5).
 *
 * `task_list` and `task_ready` in parallel, then one `task_get` per blocker
 * from outside the project, because the chart has to know whether each is
 * still open and `task_list` by project cannot say. Outside blockers are few
 * by construction, where the board's graph-wide status reads would drag every
 * live task along for them. A `null` (deleted) or closed one is dropped: it
 * holds nothing back, by `task_ready`'s own resolver.
 */
async function readWaves(slug: string): Promise<Waves> {
	const [tasks, ready] = await Promise.all([
		// Uncapped by project; see `readRollup` for why no `limit`.
		taskList({ repo: "", project_slug: slug }),
		// `task_ready` sorts and then truncates, so its default of 20 would
		// report most of wave 0 as disagreeing with it.
		taskReady({ repo: "", project_slug: slug, limit: TASK_LIMIT }),
	]);
	const inProject = new Set(tasks.map((task) => task.slug));
	const elsewhere = new Set(
		tasks
			.filter((task) => task.status !== "closed")
			.flatMap((task) => task.blocked_by ?? [])
			.filter((blocker) => !inProject.has(blocker)),
	);
	const fetched = await Promise.all(
		[...elsewhere].map((blocker) => taskGet(blocker)),
	);
	const outside = fetched.filter(
		(task): task is TaskDetail => task !== null && task.status !== "closed",
	);
	return { tasks, ready, outside };
}

/** The route fields that are arguments to the graph's reads. */
type GraphScope = Pick<Route, "repo" | "project">;

/**
 * The reads behind the Graph tab (spec §6.8): the two `witan graph` makes.
 *
 * Active projects, as the CLI's default `--status active` has it, and
 * every task in scope with closed ones among them: whether closed tasks are
 * DRAWN is the route's Closed filter, applied in the browser by `scopeTasks`
 * so toggling it does not re-read. A project narrows both reads to that one
 * project, which the CLI has no flag for.
 */
async function readGraph(scope: GraphScope): Promise<GraphData> {
	if (scope.project) {
		const slug = scope.project;
		const [projects, tasks] = await Promise.all([
			// Every status: the project was named, so a completed one is still
			// the one asked for. The active-only default would drop it, and
			// `scopeTasks` would drop every task with it.
			workflowProjectList({ repo: "", status: null }),
			// Uncapped by project; see `readRollup` for why no `limit`.
			taskList({ repo: "", project_slug: slug }),
		]);
		return {
			projects: projects.filter((project) => project.slug === slug),
			tasks,
			truncated: false,
		};
	}
	const [projects, tasks] = await Promise.all([
		workflowProjectList({ repo: scope.repo }),
		taskList({ repo: scope.repo, limit: TASK_LIMIT }),
	]);
	return { projects, tasks, truncated: tasks.length >= TASK_LIMIT };
}

/** The route fields that are arguments to the Bridge's graph read. */
interface BridgeScope {
	repo: string;
	kind: Route["contract"];
	precise: boolean;
}

/**
 * The repo graph (ADR 0011, 2026-09-23 amendment).
 *
 * `repo` is a SUBSTRING filter in this tool, so passing the route's canonical
 * URI still keeps the edges of any repo whose URI contains it; `visibleEdges`
 * narrows to exact matches. Passed anyway, because it trims what crosses the
 * wire. `""` is every repo, as everywhere else.
 */
function readBridge(scope: BridgeScope): Promise<RepoDependencies> {
	return codeRepoDependencies({
		repo: scope.repo,
		...(scope.kind ? { kind: scope.kind } : {}),
		min_precision: scope.precise ? "precise" : "heuristic",
	});
}

interface DrillScope {
	edge: string;
	binding: string;
	/** The route's confidence floor; see `producingRows`. */
	floor: number;
}

/**
 * One contract's bindings on one edge: the provider rows from the provider
 * repo and the consumer rows from the consumer repo (see `drillPlan` for the
 * service case). Both tools span every repo, so the narrowing is here.
 */
async function readDrill(scope: DrillScope): Promise<InterfaceBinding[]> {
	const edge = parseEdgeKey(scope.edge);
	const contract = parseContractKey(scope.binding);
	if (!edge || !contract) {
		return [];
	}
	const plan = drillPlan(edge, contract);
	const [providers, consumers] = await Promise.all([
		plan.providers
			? codeInterfaceProviders({
					kind: plan.providers.kind,
					key: plan.providers.key,
				})
			: Promise.resolve([]),
		plan.consumers
			? codeInterfaceConsumers({
					kind: plan.consumers.kind,
					key: plan.consumers.key,
				})
			: Promise.resolve([]),
	]);
	return producingRows(
		[
			...providers.filter((row) => row.repo === plan.providers?.repo),
			...consumers.filter((row) => row.repo === plan.consumers?.repo),
		],
		scope.floor,
	);
}

/** The route fields that are arguments to the memory view's read. */
type MemoryScope = Pick<
	Route,
	"repo" | "q" | "kind" | "plain" | "superseded" | "topic"
>;

/**
 * The read behind the memory view (spec §6.7), which depends on its state.
 *
 * A topic shows that topic; a query searches, through `recall` unless the
 * plain toggle asks for `memory_search`; neither is the landing state, which
 * is the contradictions inbox over a browse list.
 *
 * ★ `topic_get` TAKES NO REPO, KIND OR SUPERSEDED FLAG. A topic is the
 * cross-repo join surface by design, so its list ignores the repo filter; the
 * kind is applied here, and the view hides the superseded toggle.
 */
async function readMemoryPage(scope: MemoryScope): Promise<MemoryPage> {
	const { repo, q, plain, superseded } = scope;
	const kind = scope.kind ?? undefined;
	if (scope.topic) {
		const result = await topicGet(scope.topic);
		// `topic_get` takes no kind either, so the Kind select narrows here.
		const memories = (result?.memories ?? []).filter(
			(memory) => !kind || memory.kind === kind,
		);
		return { mode: "topic", topic: result?.topic ?? null, memories };
	}
	if (q && plain) {
		const memories = await memorySearch({
			query: q,
			repo,
			kind,
			include_superseded: superseded,
		});
		return { mode: "plain", memories };
	}
	if (q) {
		const result = await recall({
			query: q,
			repo,
			kind,
			include_superseded: superseded,
		});
		return {
			mode: "recall",
			memories: result.memories,
			pairs: result.contradictions,
		};
	}
	const [memories, pairs] = await Promise.all([
		memoryList({ repo, kind, include_superseded: superseded }),
		// Never `include_superseded`: a pair with a superseded side is resolved,
		// and the inbox is the list of what still needs a person.
		memoryContradictions({ repo }),
	]);
	// Each side carries its own `content`, so the inbox is this one read, not
	// one more per memory in it on every poll.
	return { mode: "browse", memories, inbox: pairs };
}

/** One memory and its neighbours, together: the panel draws both or neither. */
async function readMemoryPanel(slug: string): Promise<MemoryPanel> {
	const [memory, neighbors] = await Promise.all([
		memoryGet(slug, { topics: true }),
		memoryNeighbors({ slug }),
	]);
	return { memory, neighbors };
}

/** Every repo any project names, sorted, for the repo filter. */
export function reposIn(projects: WorkflowProjectSummary[]): string[] {
	const seen = new Set<string>();
	for (const project of projects) {
		for (const repo of project.repos ?? []) {
			seen.add(repo);
		}
	}
	return [...seen].sort();
}
