import { html, nothing, render, type TemplateResult } from "lit-html";
import { emptyBox, placeholderFor, readStatus } from "./chrome.js";
import { DAY } from "./format.js";
import { LiveRead, type Snapshot } from "./live.js";
import {
	taskGet,
	taskList,
	taskReady,
	workflowProjectGet,
	workflowProjectList,
	workflowProjectStatus,
	workflowSessionList,
} from "./mcp.js";
import { formatRoute, parseRoute, type Route } from "./route.js";
import { detailPanel, shell } from "./shell.js";
import type { TaskDetail, WorkflowProjectSummary } from "./types.js";
import { type Board, board, TASK_LIMIT } from "./views/board.js";
import { projectList, projectRollup, type Rollup } from "./views/projects.js";
import { taskDetail, taskMissing } from "./views/task-detail.js";
import { type Timeline, timeline } from "./views/timeline.js";

/**
 * The wiring: route in, reads out, one render.
 *
 * Every other module in the app is a pure function or a class with no opinion
 * about the DOM. This is the one place that owns mutable state, and it holds
 * exactly three things — the route, and one `LiveRead` per read the current
 * route needs.
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

	private rollup: LiveRead<Rollup> | null = null;
	private rollupSnapshot: Snapshot<Rollup> | null = null;
	/**
	 * The arguments `rollup` is currently reading, so an unrelated route change
	 * does not restart it.
	 *
	 * ★ WITHOUT THIS, OPENING A TASK RE-READS THE PROJECT. Every navigation
	 * comes through one `hashchange`, and a retarget blanks the snapshot by
	 * design, so a route change that touched only `slug` would flash the whole
	 * rollup back to "Reading this project…" — on every open and every close.
	 */
	private rollupKey: string | null = null;

	private board: LiveRead<Board> | null = null;
	private boardSnapshot: Snapshot<Board> | null = null;
	/** The scope `board` is reading, keyed the same way as `rollupKey`. */
	private boardKey: string | null = null;

	private timeline: LiveRead<Timeline> | null = null;
	private timelineSnapshot: Snapshot<Timeline> | null = null;
	/** The scope `timeline` is reading, keyed the same way as `rollupKey`. */
	private timelineKey: string | null = null;

	private detail: LiveRead<TaskDetail | null> | null = null;
	private detailSnapshot: Snapshot<TaskDetail | null> | null = null;
	private detailKey: string | null = null;

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
	}

	stop(): void {
		window.removeEventListener("hashchange", this.onHashChange);
		document.removeEventListener("keydown", this.onKeyDown);
		this.projects.stop();
		this.rollup?.stop();
		this.board?.stop();
		this.timeline?.stop();
		this.detail?.stop();
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
	 * Retargeting rather than rebuilding, and only when that read's own
	 * arguments changed: a rebuild would restart the poll clock, and a retarget
	 * blanks the snapshot, so reacting to every route change would re-read a
	 * project because a task was opened beside it.
	 */
	private syncReads(): void {
		this.syncRollup();
		this.syncBoard();
		this.syncTimeline();
		this.syncDetail();
	}

	private syncRollup(): void {
		// Only the Projects tab draws a rollup. Keyed on the project alone, a
		// route like `#board?project=wp-x` kept four tool calls going every 30
		// seconds behind a tab that does not draw it, and showed a read
		// time for data nothing was rendering.
		const slug = this.route.view === "projects" ? this.route.project : null;
		if (!slug) {
			this.rollup?.stop();
			this.rollup = null;
			this.rollupSnapshot = null;
			this.rollupKey = null;
			return;
		}
		// The project slug is the WHOLE key: nothing else the route carries is
		// an argument to any of the four reads. The repo used to be here, on the
		// belief that it narrowed the task list; it does not (see `readRollup`),
		// so keying on it only bought a pointless re-read of four tools.
		if (this.rollup && slug === this.rollupKey) {
			return;
		}
		const read = () => readRollup(slug);
		this.rollupKey = slug;
		if (this.rollup) {
			this.rollup.retarget(read);
			return;
		}
		this.rollup = new LiveRead(read, (snapshot) => {
			this.rollupSnapshot = snapshot;
			this.draw();
		});
		this.rollupSnapshot = this.rollup.snapshot;
		this.rollup.start();
	}

	private syncBoard(): void {
		if (this.route.view !== "board") {
			this.board?.stop();
			this.board = null;
			this.boardSnapshot = null;
			this.boardKey = null;
			return;
		}
		// Every argument any of the board's reads takes, and nothing else, so
		// opening a card in the panel does not re-read five tools.
		const scope = {
			repo: this.route.repo,
			project: this.route.project,
			closed: this.route.closed,
		};
		const key = JSON.stringify(scope);
		if (this.board && key === this.boardKey) {
			return;
		}
		const read = () => readBoard(scope);
		this.boardKey = key;
		if (this.board) {
			this.board.retarget(read);
			return;
		}
		this.board = new LiveRead(read, (snapshot) => {
			this.boardSnapshot = snapshot;
			this.draw();
		});
		this.boardSnapshot = this.board.snapshot;
		this.board.start();
	}

	private syncTimeline(): void {
		if (this.route.view !== "timeline") {
			this.timeline?.stop();
			this.timeline = null;
			this.timelineSnapshot = null;
			this.timelineKey = null;
			return;
		}
		// The repo is not in the key: both reads are repo-wide (see
		// `readTimeline`) and the repo narrows them in the browser.
		const scope = { project: this.route.project, days: this.route.days };
		const key = JSON.stringify(scope);
		if (this.timeline && key === this.timelineKey) {
			return;
		}
		const read = () => readTimeline(scope);
		this.timelineKey = key;
		if (this.timeline) {
			this.timeline.retarget(read);
			return;
		}
		// No interval (spec §6.6): it plots elapsed time, so a poll every 30
		// seconds would only move the right edge. Focus and Refresh still read.
		this.timeline = new LiveRead(
			read,
			(snapshot) => {
				this.timelineSnapshot = snapshot;
				this.draw();
			},
			{ intervalMs: 0 },
		);
		this.timelineSnapshot = this.timeline.snapshot;
		this.timeline.start();
	}

	private syncDetail(): void {
		const slug = this.route.slug;
		if (!slug) {
			this.detail?.stop();
			this.detail = null;
			this.detailSnapshot = null;
			this.detailKey = null;
			return;
		}
		if (this.detail && slug === this.detailKey) {
			return;
		}
		const read = () => taskGet(slug);
		this.detailKey = slug;
		if (this.detail) {
			this.detail.retarget(read);
			return;
		}
		this.detail = new LiveRead(read, (snapshot) => {
			this.detailSnapshot = snapshot;
			this.draw();
		});
		this.detailSnapshot = this.detail.snapshot;
		this.detail.start();
	}

	/** The reads behind whatever the main area is showing, for the status line. */
	private primary(): { snapshot: Snapshot<unknown>; refresh: () => void } {
		if (this.board && this.boardSnapshot) {
			const live = this.board;
			return {
				snapshot: this.boardSnapshot,
				refresh: () => live.refresh(),
			};
		}
		if (this.timeline && this.timelineSnapshot) {
			const live = this.timeline;
			return {
				snapshot: this.timelineSnapshot,
				refresh: () => live.refresh(),
			};
		}
		if (this.rollup && this.rollupSnapshot) {
			const rollup = this.rollup;
			return {
				snapshot: this.rollupSnapshot,
				refresh: () => rollup.refresh(),
			};
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
			}),
			this.root,
		);

		this.moveFocus();
	}

	private body(inScope: WorkflowProjectSummary[]): TemplateResult {
		if (this.route.view === "board") {
			const snapshot = this.boardSnapshot;
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
			const snapshot = this.timelineSnapshot;
			if (!snapshot) {
				return emptyBox("Reading the timeline…");
			}
			const waiting = placeholderFor(snapshot, "the timeline");
			if (waiting) {
				return waiting;
			}
			// All projects, not `inScope`: a task in the repo can belong to a
			// project that does not list it, and its group still needs a title.
			return timeline(
				snapshot.data as Timeline,
				this.route,
				this.projectsSnapshot.data ?? [],
			);
		}

		if (this.route.view !== "projects") {
			// Named rather than blank: the tabs are declared up front (spec §6) so
			// the shell has one navigation model, and a person landing on an
			// unbuilt tab should learn that it is unbuilt, not that it is empty.
			return emptyBox(`The ${this.route.view} view is not built yet.`);
		}

		if (this.route.project) {
			const snapshot = this.rollupSnapshot;
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

	private panel(): TemplateResult | typeof nothing {
		const slug = this.route.slug;
		if (!slug) {
			return nothing;
		}
		const snapshot = this.detailSnapshot;
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
		const detail = this.detail;
		const status = detail
			? readStatus(snapshot, () => detail.refresh())
			: nothing;
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
async function readBoard(scope: {
	repo: string;
	project: string | null;
	closed: boolean;
}): Promise<Board> {
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

/**
 * The reads behind the timeline (spec §6.6), all-or-nothing like the others.
 *
 * Tasks are read whole and windowed in the browser: `task_list` has no time
 * filter, and a task created long before the window can still be open or have
 * closed inside it. Sessions are windowed by the server through `since`, which
 * is what keeps the read from growing with every session ever recorded
 * (spec §3.7). Across every repo, for the reason the board's live read is.
 */
async function readTimeline(scope: {
	project: string | null;
	days: number;
}): Promise<Timeline> {
	const readAt = Date.now();
	const since = new Date(readAt - scope.days * DAY).toISOString();
	const [tasks, sessions] = await Promise.all([
		// `project_slug` returns early and uncapped; see `readRollup` for why a
		// limit there would only drop rows.
		scope.project
			? taskList({ repo: "", project_slug: scope.project })
			: taskList({ repo: "", limit: TASK_LIMIT }),
		workflowSessionList(
			scope.project ? { since, project_slug: scope.project } : { since },
		),
	]);
	return {
		tasks,
		sessions,
		readAt,
		days: scope.days,
		truncated: !scope.project && tasks.length >= TASK_LIMIT,
	};
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
