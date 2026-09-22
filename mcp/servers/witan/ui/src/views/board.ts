import { html, nothing, type TemplateResult } from "lit-html";
import { emptyBox } from "../chrome.js";
import { absolute, ago, parseTimestamp, repoLabel } from "../format.js";
import { type Route, routeHref } from "../route.js";
import type { TaskPriority, TaskRow, TaskStatus } from "../types.js";

/**
 * The Board (spec §6.4): Ready, In progress, Blocked and, when asked for,
 * Closed.
 *
 * Pure over data the app has already read, like the Projects view. The one
 * rule that is NOT here is readiness: Ready is `task_ready`'s result drawn in
 * the order it came back, and Blocked is only "not in that result". A second
 * copy of the rule in the browser is a copy that drifts, and
 * `readiness.filter_ready` in particular treats a blocker missing from its set
 * as closed, which is wrong for any scoped view.
 */

/**
 * `task_list`'s ceiling (server.py `_MAX_TASK_LIMIT`), passed to every read.
 *
 * Passing a limit at all is what lifts the unscoped reads off their silent
 * 50-row cap. Passing it to `task_ready` too is what keeps Ready honest:
 * `task_ready` sorts and THEN truncates, so a smaller limit would push ready
 * tasks into Blocked.
 */
export const TASK_LIMIT = 10_000;

/** How many closed cards to draw, newest first. The count shows the rest. */
export const CLOSED_SHOWN = 50;

/** What the board reads, as the app hands it over. */
export interface Board {
	/** `task_ready` for the route's scope. */
	ready: TaskRow[];
	/**
	 * Every non-closed task in the GRAPH, across all repos.
	 *
	 * ★ ALL REPOS, AND THAT IS THE POINT. A blocked card has to name its open
	 * blockers, and a blocker is routinely in another repo or project. Only a
	 * read wider than the scope can tell "open, elsewhere" from "closed", and
	 * any task absent from this set is closed or gone, so neither holds
	 * anything back. The columns are narrowed from it by `inScope`.
	 */
	live: TaskRow[];
	/** Closed tasks in scope, or `null` when the Closed filter is off. */
	closed: TaskRow[] | null;
	/** A read came back at `TASK_LIMIT` rows, so there may be more. */
	truncated: boolean;
}

/**
 * Whether a task belongs to the route's scope, by the rule the tools apply.
 *
 * A project wins over a repo, as `project_slug` does in `task_list` and
 * `task_ready`. A repo matches its own tasks AND the unscoped ones, because
 * both tools' repo branch reads `list_tasks_by_repo` plus the `repo = null`
 * rows. Scoping the columns by any other rule than Ready was read with would
 * leave ready tasks in Blocked, or blocked ones nowhere.
 */
export function inScope(task: TaskRow, route: Route): boolean {
	if (route.project) {
		return task.project_slug === route.project;
	}
	if (route.repo) {
		return task.repo === route.repo || task.repo === null;
	}
	return true;
}

/** Columns as drawn, plus the lookup blocked cards resolve their blockers in. */
export interface Columns {
	ready: TaskRow[];
	inProgress: TaskRow[];
	blocked: TaskRow[];
	closed: TaskRow[] | null;
	live: Map<string, TaskRow>;
}

const PRIORITY_RANK: Record<TaskPriority, number> = {
	p0: 0,
	p1: 1,
	p2: 2,
	p3: 3,
};

/** Epoch ms of a timestamp the tools returned, `0` when absent. */
function at(value: string | null | undefined): number {
	return parseTimestamp(value)?.getTime() ?? 0;
}

/**
 * When the current lease started: `claimed_at`, else `updated_at`.
 *
 * The same fallback `readiness.status_pickable` uses for a row with no
 * `claimed_at` (a legacy claim), so the age drawn and the server's
 * `lease_expired` measure from the same instant and cannot disagree.
 */
export function leaseStart(task: TaskRow): string {
	return task.claimed_at ?? task.updated_at;
}

export function columns(board: Board, route: Route): Columns {
	const live = new Map(board.live.map((task) => [task.slug, task]));
	const ready = new Set(board.ready.map((task) => task.slug));
	const scoped = board.live.filter((task) => inScope(task, route));

	return {
		ready: board.ready,
		// Longest-held first: an old claim is the one a person opens the board
		// to find, and a lapsed one is necessarily among the oldest.
		inProgress: scoped
			.filter((task) => task.status === "in_progress")
			.sort((a, b) => at(leaseStart(a)) - at(leaseStart(b))),
		blocked: scoped
			.filter(
				(task) =>
					(task.status === "open" || task.status === "blocked") &&
					!ready.has(task.slug),
			)
			.sort(
				(a, b) =>
					PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority] ||
					at(b.updated_at) - at(a.updated_at),
			),
		closed: board.closed
			? [...board.closed].sort((a, b) => at(b.closed_at) - at(a.closed_at))
			: null,
		live,
	};
}

/** The blockers of `task` that are not closed, from the graph-wide read. */
export function openBlockers(
	task: TaskRow,
	live: Map<string, TaskRow>,
): TaskRow[] {
	return (task.blocked_by ?? []).flatMap((slug) => {
		const blocker = live.get(slug);
		return blocker ? [blocker] : [];
	});
}

export function board(
	data: Board,
	route: Route,
	now = Date.now(),
): TemplateResult {
	const cols = columns(data, route);
	const card = (task: TaskRow) => boardCard(task, cols.live, route, now);
	return html`
    ${
			data.truncated
				? html`<p class="note">
            A read came back at its ${TASK_LIMIT}-row limit, so this board may
            be missing tasks.
          </p>`
				: nothing
		}
    <div class="board">
      ${column("Ready", cols.ready, card, "Nothing is ready to pick up.")}
      ${column(
				"In progress",
				cols.inProgress,
				card,
				"Nobody is holding a task.",
			)}
      ${column("Blocked", cols.blocked, card, "Nothing is blocked.")}
      ${cols.closed ? closedColumn(cols.closed, card) : nothing}
    </div>
  `;
}

function column(
	title: string,
	tasks: TaskRow[],
	card: (task: TaskRow) => TemplateResult,
	empty: string,
): TemplateResult {
	return html`
    <section class="column" aria-label=${title}>
      <h3>${title} <span class="count">${tasks.length}</span></h3>
      ${
				tasks.length === 0
					? emptyBox(empty)
					: html`<ul class="cards">
              ${tasks.map(card)}
            </ul>`
			}
    </section>
  `;
}

function closedColumn(
	tasks: TaskRow[],
	card: (task: TaskRow) => TemplateResult,
): TemplateResult {
	const shown = tasks.slice(0, CLOSED_SHOWN);
	return html`
    <section class="column" aria-label="Closed">
      <h3>
        Closed
        <span class="count"
          >${
						tasks.length > shown.length
							? `newest ${shown.length} of ${tasks.length}`
							: tasks.length
					}</span
        >
      </h3>
      ${
				tasks.length === 0
					? emptyBox("Nothing has been closed.")
					: html`<ul class="cards">
              ${shown.map(card)}
            </ul>`
			}
    </section>
  `;
}

const STATUS_LABEL: Record<TaskStatus, string> = {
	open: "open",
	in_progress: "in progress",
	blocked: "blocked",
	closed: "closed",
};

/**
 * One task on the board.
 *
 * The claim line is the part with no beads equivalent. An in-progress card
 * always says who holds it and since when, and says "claim lapsed" exactly
 * when the server's `lease_expired` does. The UI never compares the age to a
 * lease length of its own, so the mark and the server's reclaim rule cannot
 * disagree.
 */
export function boardCard(
	task: TaskRow,
	live: Map<string, TaskRow>,
	route: Route,
	now = Date.now(),
): TemplateResult {
	const blockers = task.status === "closed" ? [] : openBlockers(task, live);
	return html`
    <li class=${task.lease_expired ? "card stale" : "card"}>
      <p class="card-head">
        <span class="badge priority">${task.priority}</span>
        <a href=${routeHref(route, { slug: task.slug })}>${task.title}</a>
      </p>
      <p class="card-meta">
        ${task.type}
        ${
					/* The repo is noise when the scope already names one. */
					!route.repo && !route.project && task.repo
						? html`· <span title=${task.repo}>${repoLabel(task.repo)}</span>`
						: nothing
				}
        ${
					task.status === "closed"
						? html`· <span title=${absolute(task.closed_at)}
                  >closed ${ago(task.closed_at, now)}</span
                >`
						: nothing
				}
      </p>
      ${claimLine(task, now)}
      ${
				task.status === "blocked" || blockers.length > 0
					? blockerLine(task, blockers, route)
					: nothing
			}
    </li>
  `;
}

function claimLine(
	task: TaskRow,
	now: number,
): TemplateResult | typeof nothing {
	if (task.status !== "in_progress") {
		return nothing;
	}
	const start = leaseStart(task);
	return html`
    <p class="claim">
      <span class="assignee">${task.assignee ?? "unassigned"}</span>
      <span title=${absolute(start)}>claimed ${ago(start, now)}</span>
      ${
				task.lease_expired
					? html`<span class="badge stale-claim">claim lapsed</span>`
					: nothing
			}
    </p>
  `;
}

/**
 * "Blocked" without "on what" is not actionable, so a blocked card names each
 * open blocker, links it, and says where it stands.
 *
 * A card can land in Blocked with no open blocker found when the reads raced a
 * close, or when a task is marked `blocked` with no edge at all. Both say so
 * rather than rendering an empty line.
 */
function blockerLine(
	task: TaskRow,
	blockers: TaskRow[],
	route: Route,
): TemplateResult {
	if (blockers.length === 0) {
		return html`<p class="blockers muted">
      ${
				task.blocked_by?.length
					? "No open blocker in the last read; refresh."
					: "Marked blocked, with no blocker linked."
			}
    </p>`;
	}
	return html`
    <ul class="blockers" aria-label="Open blockers">
      ${blockers.map(
				(blocker) => html`
          <li>
            waits on
            <a href=${routeHref(route, { slug: blocker.slug })}>${blocker.title}</a>
            <span class="badge">${STATUS_LABEL[blocker.status]}</span>
          </li>
        `,
			)}
    </ul>
  `;
}
