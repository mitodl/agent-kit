import { html, nothing, svg, type TemplateResult } from "lit-html";
import { emptyBox } from "../chrome.js";
import { absolute, DAY, duration, parseTimestamp } from "../format.js";
import { type Route, routeHref, WINDOW_DAYS } from "../route.js";
import type {
	TaskRow,
	WorkflowProjectSummary,
	WorkflowSession,
} from "../types.js";
import { inScope, leaseStart, TASK_LIMIT } from "./board.js";

/**
 * The retrospective timeline (spec §3.6, §6.6): where the window went.
 *
 * Built only from timestamps that mean what they say. `claimed_at` is NOT one
 * of them as a work start: `task_claim` rewrites it on every lease renewal and
 * `task_release` nulls it, so a bar drawn from it would start at the last
 * renewal. Each task gets:
 *
 * - a lead-time bar, `created_at` to `closed_at` (or to the read, if open);
 * - a work-time segment inside it, from `first_claimed_at`, where that exists;
 * - a "current lease since" mark at `claimed_at`, on in-progress tasks only.
 *
 * `first_claimed_at` has no backfill, so a task without one is drawn hatched
 * rather than given a guessed work start.
 *
 * ★ SVG GEOMETRY, NOT CSS POSITIONS. `/ui/` is served with
 * `default-src 'self'` (`ui_routes.py` `_security_headers`), which blocks
 * inline `style=` attributes, so a bar positioned with `left:`/`width:` would
 * render at zero. SVG `x`/`width` are attributes, not styles. It also needs no
 * script from anywhere, which is what lets the MCP Apps widget reuse it.
 */

/** What the timeline reads, as the app hands it over. */
export interface Timeline {
	/** Every task in the read's scope. Filtered to the window here, not by the server. */
	tasks: TaskRow[];
	/** Sessions still open or ended at or after `since`, from `workflow_session_list`. */
	sessions: WorkflowSession[];
	/**
	 * When the reads were issued, as epoch ms: the window's right edge.
	 *
	 * Fixed at read time rather than taken from the clock at render, so an
	 * open task's bar ends where the data it was drawn from ends, and a
	 * re-render with no re-read does not quietly stretch every open bar.
	 */
	readAt: number;
	/** The window's width in days, back from `readAt`. */
	days: number;
	/** The task read came back at `TASK_LIMIT` rows, so there may be more. */
	truncated: boolean;
}

/** Epoch ms bounds. Unclipped: the chart clips when it draws. */
export interface Span {
	start: number;
	end: number;
}

export interface TaskBar {
	task: TaskRow;
	/** `created_at` to `closed_at`, or to the read for a task not closed. */
	lead: Span;
	/** `first_claimed_at` to the lead bar's end, or `null` when unrecorded. */
	work: Span | null;
	/** The current lease's start, on an in-progress task only. */
	lease: number | null;
}

export interface SessionSpan {
	session: WorkflowSession;
	span: Span;
	/**
	 * Never ended, so `span` is its start alone.
	 *
	 * ★ NOT DRAWN TO THE READ. Nothing makes an agent call
	 * `workflow_session_end`, so an open session is as often abandoned as
	 * running, and a bar to now would claim weeks of work that never happened.
	 * Against a real graph those bars filled every lane of every project.
	 */
	open: boolean;
}

export interface Group {
	/** The `wp-` slug, or `null` for tasks and sessions with no project. */
	project: string | null;
	title: string;
	bars: TaskBar[];
	/** Sessions packed into lanes so parallel sessions do not overdraw. */
	lanes: SessionSpan[][];
}

export interface Layout {
	window: Span;
	groups: Group[];
	/** Open tasks never claimed, left out of the chart. */
	backlog: number;
	/** Closed tasks with no `closed_at`, which have no end to draw to. */
	undated: number;
}

function ms(value: string | null | undefined): number | null {
	return parseTimestamp(value)?.getTime() ?? null;
}

/**
 * A task's bar, or why it has none.
 *
 * ★ AN OPEN TASK NOBODY EVER CLAIMED IS NOT DRAWN. Its only start is
 * `created_at`, so its bar would run from whenever it was filed to now and
 * look exactly like work in flight. Across every project that is most of the
 * chart, and all of it backlog. It is counted instead.
 *
 * "Never claimed" is `first_claimed_at` AND `claimed_at` both null on a task
 * not in progress. A task released before `first_claimed_at` shipped is
 * indistinguishable from one never claimed, and lands here too.
 */
export function taskBar(
	task: TaskRow,
	readAt: number,
): TaskBar | "backlog" | "undated" {
	const created = ms(task.created_at) ?? readAt;
	const firstClaimed = ms(task.first_claimed_at);
	if (task.status === "closed") {
		// `closed_at` is read only on a closed row: before the invariant fix, a
		// reopened task kept its old `closed_at` (spec §3.6).
		const closed = ms(task.closed_at);
		if (closed === null) {
			return "undated";
		}
		return {
			task,
			lead: { start: created, end: closed },
			work:
				firstClaimed !== null && firstClaimed <= closed
					? { start: firstClaimed, end: closed }
					: null,
			lease: null,
		};
	}
	if (
		task.status !== "in_progress" &&
		firstClaimed === null &&
		task.claimed_at === null
	) {
		return "backlog";
	}
	return {
		task,
		lead: { start: created, end: readAt },
		work: firstClaimed !== null ? { start: firstClaimed, end: readAt } : null,
		lease:
			task.status === "in_progress" ? (ms(leaseStart(task)) ?? null) : null,
	};
}

/**
 * Pack sessions into as few lanes as keep them from overlapping.
 *
 * Greedy by start time: each session goes in the first lane whose last
 * session has ended. Parallel sessions on one project are routine, and drawn
 * in one lane the later one would hide the earlier.
 */
export function packLanes(spans: SessionSpan[]): SessionSpan[][] {
	const lanes: SessionSpan[][] = [];
	const sorted = [...spans].sort(
		(a, b) => a.span.start - b.span.start || a.span.end - b.span.end,
	);
	for (const span of sorted) {
		const lane = lanes.find(
			(candidate) => (candidate.at(-1)?.span.end ?? 0) <= span.span.start,
		);
		if (lane) {
			lane.push(span);
		} else {
			lanes.push([span]);
		}
	}
	return lanes;
}

/**
 * Whether a session belongs to the route's scope.
 *
 * Sessions carry no repo, only a project, so a repo filter keeps the sessions
 * of projects that name the repo. A project-less session has nothing to match
 * a repo on and is kept only when no repo is selected.
 */
function sessionInScope(
	session: WorkflowSession,
	route: Route,
	projects: Map<string, WorkflowProjectSummary>,
): boolean {
	if (route.project) {
		return session.project_slug === route.project;
	}
	if (!route.repo) {
		return true;
	}
	if (!session.project_slug) {
		return false;
	}
	return (projects.get(session.project_slug)?.repos ?? []).includes(route.repo);
}

const NO_PROJECT = "No project";

/**
 * Everything the chart draws, as data.
 *
 * `projects` supplies group titles and the repo each session's project names.
 * A slug missing from it (a project read not landed yet, or one since
 * deleted) is titled by its slug rather than dropped.
 */
export function layout(
	data: Timeline,
	route: Route,
	projects: WorkflowProjectSummary[],
): Layout {
	const window = { start: data.readAt - data.days * DAY, end: data.readAt };
	const bySlug = new Map(projects.map((project) => [project.slug, project]));
	const groups = new Map<string | null, Group>();
	const group = (slug: string | null): Group => {
		let found = groups.get(slug);
		if (!found) {
			found = {
				project: slug,
				title: slug ? (bySlug.get(slug)?.title ?? slug) : NO_PROJECT,
				bars: [],
				lanes: [],
			};
			groups.set(slug, found);
		}
		return found;
	};

	let backlog = 0;
	let undated = 0;
	for (const task of data.tasks) {
		if (!inScope(task, route)) {
			continue;
		}
		const bar = taskBar(task, data.readAt);
		if (bar === "backlog") {
			backlog += 1;
			continue;
		}
		if (bar === "undated") {
			undated += 1;
			continue;
		}
		if (bar.lead.end < window.start) {
			continue;
		}
		group(task.project_slug).bars.push(bar);
	}

	const sessions = new Map<string | null, SessionSpan[]>();
	for (const session of data.sessions) {
		if (!sessionInScope(session, route, bySlug)) {
			continue;
		}
		const start = ms(session.started_at);
		if (start === null) {
			continue;
		}
		const ended = ms(session.ended_at);
		const span = { start, end: ended ?? start };
		// An open session is only a start, so one that started before the
		// window has nothing inside it to draw.
		if (span.end < window.start) {
			continue;
		}
		const key = session.project_slug;
		sessions.set(key, [
			...(sessions.get(key) ?? []),
			{ session, span, open: ended === null },
		]);
	}
	for (const [slug, spans] of sessions) {
		group(slug).lanes = packLanes(spans);
	}

	for (const found of groups.values()) {
		found.bars.sort(
			(a, b) =>
				a.lead.start - b.lead.start || a.task.slug.localeCompare(b.task.slug),
		);
	}
	return {
		window,
		groups: [...groups.values()].sort(
			(a, b) =>
				Number(a.project === null) - Number(b.project === null) ||
				a.title.localeCompare(b.title),
		),
		backlog,
		undated,
	};
}

// ── Drawing ─────────────────────────────────────────────────────────

/**
 * A row's height in px.
 *
 * Horizontal positions are PERCENTAGES of the track instead, which SVG lengths
 * accept, so every track stretches to its column without a viewBox. A
 * stretched viewBox (`preserveAspectRatio="none"`) would do it too, but it
 * stretches the axis labels and the hatching along with the bars.
 */
const ROW = 18;

/** Where an instant falls across the window, as a percentage, clipped. */
function scale(window: Span): (at: number) => number {
	const width = window.end - window.start;
	return (at) =>
		Math.min(100, Math.max(0, ((at - window.start) / width) * 100));
}

function pct(value: number): string {
	return `${value.toFixed(3)}%`;
}

/** UTC midnights inside the window, thinned so their labels do not collide. */
export function ticks(window: Span, days: number): number[] {
	const step = days <= 14 ? 1 : days <= 30 ? 3 : 7;
	const first = Math.ceil(window.start / DAY) * DAY;
	const found: number[] = [];
	for (let at = first; at <= window.end; at += step * DAY) {
		found.push(at);
	}
	return found;
}

const DATE_LABEL = new Intl.DateTimeFormat("en-US", {
	month: "short",
	day: "numeric",
	timeZone: "UTC",
});

export function timeline(
	data: Timeline,
	route: Route,
	projects: WorkflowProjectSummary[],
): TemplateResult {
	const plan = layout(data, route, projects);
	const x = scale(plan.window);
	const marks = ticks(plan.window, data.days);
	return html`
    <section class="timeline" aria-label="Timeline">
      <header class="timeline-head">
        <p class="note">
          What happened, not a plan: each bar is a task's lead time, the solid
          part is time since it was first claimed, and no bar is a commitment.
        </p>
        <nav class="window" aria-label="Window">
          ${WINDOW_DAYS.map(
						(days) => html`<a
              href=${routeHref(route, { days })}
              aria-current=${days === data.days ? "page" : "false"}
              >${days}d</a
            >`,
					)}
        </nav>
      </header>
      ${hatchDefs()} ${notes(data, plan)} ${legend()}
      ${
				plan.groups.length === 0
					? emptyBox(`Nothing was worked in the last ${data.days} days.`)
					: html`
              <div class="tl-row tl-axis">
                <span></span>${axis(marks, x)}
              </div>
              ${plan.groups.map((found) => groupRows(found, route, marks, x))}
            `
			}
    </section>
  `;
}

function notes(data: Timeline, plan: Layout): TemplateResult {
	return html`
    ${
			data.truncated
				? html`<p class="note">
            The task read came back at its ${TASK_LIMIT}-row limit, so this
            timeline may be missing tasks.
          </p>`
				: nothing
		}
    ${
			plan.backlog > 0
				? html`<p class="note">
            ${plan.backlog} open ${plan.backlog === 1 ? "task" : "tasks"} never
            claimed ${plan.backlog === 1 ? "is" : "are"} not drawn: a bar from
            filing to now would look like work in flight.
          </p>`
				: nothing
		}
    ${
			plan.undated > 0
				? html`<p class="note">
            ${plan.undated} closed ${plan.undated === 1 ? "task has" : "tasks have"}
            no close time and ${plan.undated === 1 ? "is" : "are"} not drawn.
          </p>`
				: nothing
		}
  `;
}

/** The hatch pattern, defined once and referenced by every hatched bar. */
function hatchDefs(): TemplateResult {
	return html`
    <svg class="tl-defs" width="0" height="0" aria-hidden="true">
      <defs>
        <pattern
          id="tl-hatch"
          patternUnits="userSpaceOnUse"
          width="6"
          height="6"
          patternTransform="rotate(45)"
        >
          <rect class="lead" width="6" height="6"></rect>
          <line class="hatch" x1="0" y1="0" x2="0" y2="6"></line>
        </pattern>
      </defs>
    </svg>
  `;
}

function legend(): TemplateResult {
	const swatch = (body: TemplateResult) =>
		html`<svg class="swatch" viewBox="0 0 24 12" aria-hidden="true">${body}</svg>`;
	return html`
    <ul class="tl-legend">
      <li>${swatch(svg`<rect class="lead" x="0" y="2" width="24" height="8"></rect>`)} lead time, filed to closed</li>
      <li>${swatch(svg`<rect class="work" x="0" y="2" width="24" height="8"></rect>`)} since first claimed</li>
      <li>${swatch(svg`<rect class="lead hatched" x="0" y="2" width="24" height="8"></rect>`)}
        no first-claim time: claimed before it was recorded, or closed unclaimed</li>
      <li>${swatch(svg`<line class="lease" x1="12" y1="0" x2="12" y2="12"></line>`)} current lease since</li>
      <li>${swatch(svg`<rect class="session" x="0" y="2" width="24" height="8"></rect>`)} session</li>
      <li>${swatch(svg`<line class="session-start" x1="12" y1="0" x2="12" y2="12"></line>`)}
        session started, never ended</li>
    </ul>
  `;
}

/**
 * The date axis.
 *
 * Every other label is classed `alternate` so a narrow screen can drop half of
 * them (`style.css`): the tick count is fixed per window, and the width is
 * not known until the page lays out.
 */
function axis(marks: number[], x: (at: number) => number): TemplateResult {
	return html`
    <svg class="tl-track" height="20" aria-hidden="true">
      ${marks.map(
				(at, index) => svg`
          <line class="grid" x1=${pct(x(at))} y1="14" x2=${pct(x(at))} y2="20"></line>
          ${axisLabel(at, index, x)}
        `,
			)}
    </svg>
  `;
}

/**
 * A date starting at its tick, or nothing for a tick near the right edge,
 * where the label would run out of the track and be clipped. Anchoring it at
 * its end instead ran it into the label before.
 */
function axisLabel(
	at: number,
	index: number,
	x: (at: number) => number,
): unknown {
	const left = x(at);
	if (left > 90) {
		return nothing;
	}
	return svg`<text class=${index % 2 ? "alternate" : ""} x=${pct(left)} dx="3" y="12"
    >${DATE_LABEL.format(at)}</text>`;
}

/** A row's track, gridlines behind its marks. */
function track(
	marks: number[],
	x: (at: number) => number,
	body: unknown,
): TemplateResult {
	return html`
    <svg class="tl-track" height=${ROW}>
      ${marks.map(
				(at) =>
					svg`<line class="grid" x1=${pct(x(at))} y1="0" x2=${pct(x(at))} y2=${ROW}></line>`,
			)}
      ${body}
    </svg>
  `;
}

/**
 * At least this wide, in percent, so a task opened and closed within the hour
 * still shows on a 90-day window.
 */
const MIN_WIDTH = 0.2;

function rect(
	cls: string,
	span: Span,
	x: (at: number) => number,
	label: string,
): unknown {
	const left = x(span.start);
	const width = Math.max(x(span.end) - left, MIN_WIDTH);
	return svg`<rect class=${cls} x=${pct(left)} y="4" width=${pct(width)} height="10"
    ><title>${label}</title></rect>`;
}

function spanText(span: Span): string {
	return `${absolute(new Date(span.start))} to ${absolute(new Date(span.end))}, ${duration(span.end - span.start)}`;
}

function groupRows(
	found: Group,
	route: Route,
	marks: number[],
	x: (at: number) => number,
): TemplateResult {
	return html`
    <section class="tl-group" aria-label=${found.title}>
      <h3>
        ${
					found.project
						? html`<a
                href=${routeHref(route, {
									view: "projects",
									project: found.project,
								})}
                >${found.title}</a
              >`
						: found.title
				}
        <span class="count">${found.bars.length}</span>
      </h3>
      ${found.bars.map((bar) => barRow(bar, route, marks, x))}
      ${found.lanes.map((lane, index) => laneRow(lane, index, marks, x))}
    </section>
  `;
}

function barRow(
	bar: TaskBar,
	route: Route,
	marks: number[],
	x: (at: number) => number,
): TemplateResult {
	const { task } = bar;
	const ending = task.status === "closed" ? "" : ", still open";
	return html`
    <div class="tl-row" data-slug=${task.slug}>
      <a class="tl-label" href=${routeHref(route, { slug: task.slug })}
        title=${task.title}>${task.title}</a>
      ${track(
				marks,
				x,
				svg`
          ${rect(
						bar.work ? "lead" : "lead hatched",
						bar.lead,
						x,
						`Lead time: ${spanText(bar.lead)}${ending}`,
					)}
          ${
						bar.work
							? rect(
									"work",
									bar.work,
									x,
									`Since first claimed: ${spanText(bar.work)}${ending}`,
								)
							: nothing
					}
          ${bar.lease !== null ? leaseMark(bar, bar.lease, x) : nothing}
        `,
			)}
    </div>
  `;
}

function leaseMark(
	bar: TaskBar,
	lease: number,
	x: (at: number) => number,
): unknown {
	const at = pct(x(lease));
	const holder = bar.task.assignee ?? "unassigned";
	const lapsed = bar.task.lease_expired ? " (lapsed)" : "";
	return svg`<line class=${bar.task.lease_expired ? "lease stale" : "lease"}
    x1=${at} y1="0" x2=${at} y2=${ROW}
    ><title>Current lease since ${absolute(new Date(lease))}, ${holder}${lapsed}</title></line>`;
}

function laneRow(
	lane: SessionSpan[],
	index: number,
	marks: number[],
	x: (at: number) => number,
): TemplateResult {
	return html`
    <div class="tl-row tl-sessions">
      <span class="tl-label muted">${index === 0 ? "Sessions" : ""}</span>
      ${track(
				marks,
				x,
				lane.map(({ session, span, open }) => {
					const label = [
						`${session.phase} session ${session.slug}`,
						open
							? `started ${absolute(new Date(span.start))}, never ended: still running, or abandoned`
							: spanText(span),
						session.summary ?? "",
					]
						.filter(Boolean)
						.join("\n");
					return open
						? svg`<line class="session-start" x1=${pct(x(span.start))} y1="2"
                x2=${pct(x(span.start))} y2=${ROW - 2}><title>${label}</title></line>`
						: rect("session", span, x, label);
				}),
			)}
    </div>
  `;
}
