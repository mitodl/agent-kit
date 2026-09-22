import { html, nothing, render, type TemplateResult } from "lit-html";
import { emptyBox, placeholderFor, readStatus } from "./chrome.js";
import { LiveRead, type Snapshot } from "./live.js";
import {
	taskGet,
	taskList,
	workflowProjectGet,
	workflowProjectList,
	workflowProjectStatus,
	workflowSessionList,
} from "./mcp.js";
import { formatRoute, parseRoute, type Route } from "./route.js";
import { detailPanel, shell } from "./shell.js";
import type { TaskDetail, WorkflowProjectSummary } from "./types.js";
import { projectList, projectRollup, type Rollup } from "./views/projects.js";
import { taskDetail, taskMissing } from "./views/task-detail.js";

/**
 * The wiring: route in, reads out, one render.
 *
 * Every other module in the app is a pure function or a class with no opinion
 * about the DOM. This is the one place that owns mutable state, and it holds
 * exactly three things — the route, and one `LiveRead` per read the current
 * route needs.
 */

/**
 * Upper bound on a project's task list.
 *
 * Passed explicitly because `task_list` silently caps an unscoped read at 50
 * (the read-gap task added the parameter for this), and a rollup that showed
 * 50 of a project's 80 tasks with no sign of the cut is worse than one that
 * asks for all of them. The server's ceiling is 10000; a project with more
 * tasks than this is not a thing the graph has.
 */
const TASK_LIMIT = 1000;

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
		this.syncDetail();
	}

	private syncRollup(): void {
		// Only the Projects tab draws a rollup. Keyed on the project alone, a
		// route like `#board?project=wp-x` kept four tool calls going every 30
		// seconds behind a tab that renders "not built yet", and showed a read
		// time for data nothing was rendering.
		const slug = this.route.view === "projects" ? this.route.project : null;
		if (!slug) {
			this.rollup?.stop();
			this.rollup = null;
			this.rollupSnapshot = null;
			this.rollupKey = null;
			return;
		}
		// The repo is in the key because it is an argument to the rollup's
		// `task_list`, so changing it does have to re-read.
		const key = `${slug}\u0000${this.route.repo}`;
		if (this.rollup && key === this.rollupKey) {
			return;
		}
		const repo = this.route.repo;
		const read = () => readRollup(slug, repo);
		this.rollupKey = key;
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
		const task = snapshot.data;
		// A null with a result behind it is the graph saying no: a stale link,
		// which is normal, rather than a failure.
		if (task === null) {
			return detailPanel(this.route, slug, taskMissing(slug));
		}
		return detailPanel(this.route, task.title, taskDetail(task, this.route));
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
async function readRollup(slug: string, repo: string): Promise<Rollup> {
	const [detail, status, tasks, sessions] = await Promise.all([
		workflowProjectGet(slug),
		workflowProjectStatus(slug),
		taskList({ repo, project_slug: slug, limit: TASK_LIMIT }),
		workflowSessionList({ project_slug: slug }),
	]);
	return { detail, status, tasks, sessions };
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
