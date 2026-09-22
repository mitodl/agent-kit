import { render } from "lit-html";
import { beforeEach, describe, expect, it } from "vitest";
import taskListFixture from "../../fixtures/task_list.json" with {
	type: "json",
};
import projectListFixture from "../../fixtures/workflow_project_list.json" with {
	type: "json",
};
import sessionListFixture from "../../fixtures/workflow_session_list.json" with {
	type: "json",
};
import { DAY } from "../format.js";
import { DEFAULT_ROUTE, type Route } from "../route.js";
import type {
	TaskRow,
	WorkflowProjectSummary,
	WorkflowSession,
} from "../types.js";
import { unwrap } from "../unwrap.js";
import {
	layout,
	packLanes,
	type SessionSpan,
	type TaskBar,
	type Timeline,
	taskBar,
	ticks,
	timeline,
} from "./timeline.js";

/**
 * The timeline, from rows DERIVED from the recorded `task_list` and
 * `workflow_session_list` results, so every field the server sends is present
 * and a renamed one still fails here. The fixtures record every timestamp at
 * one instant, so the cases below patch the timestamps they are about.
 */

const tasks = unwrap<TaskRow[]>("task_list", taskListFixture);
const sessions = unwrap<WorkflowSession[]>(
	"workflow_session_list",
	sessionListFixture,
);
const projects = unwrap<WorkflowProjectSummary[]>(
	"workflow_project_list",
	projectListFixture,
);

const AGENT_KIT = "https://github.com/mitodl/agent-kit";
const HQ = "https://github.com/mitodl/hq";
const READ_AT = Date.parse("2026-09-22T12:00:00Z");
const HOUR = 3_600_000;

function iso(at: number): string {
	return new Date(at).toISOString();
}

/** Zone-less, as the graph stores most timestamps. */
function naive(at: number): string {
	return iso(at).replace("Z", "");
}

function task(patch: Partial<TaskRow>): TaskRow {
	const base = tasks[0];
	if (!base) {
		throw new Error("task_list fixture is empty");
	}
	return {
		...base,
		status: "open",
		first_claimed_at: null,
		claimed_at: null,
		closed_at: null,
		lease_expired: undefined,
		...patch,
	};
}

function session(patch: Partial<WorkflowSession>): WorkflowSession {
	const base = sessions[0];
	if (!base) {
		throw new Error("workflow_session_list fixture is empty");
	}
	return { ...base, ...patch };
}

function data(patch: Partial<Timeline> = {}): Timeline {
	return {
		tasks: [],
		sessions: [],
		readAt: READ_AT,
		days: 14,
		truncated: false,
		projects,
		...patch,
	};
}

function bar(row: TaskRow): TaskBar {
	const result = taskBar(row, READ_AT);
	if (typeof result === "string") {
		throw new Error(`no bar: ${result}`);
	}
	return result;
}

const closed = task({
	slug: "tk-closed",
	status: "closed",
	created_at: naive(READ_AT - 5 * DAY),
	first_claimed_at: naive(READ_AT - 3 * DAY),
	closed_at: naive(READ_AT - 1 * DAY),
});
const inFlight = task({
	slug: "tk-in-flight",
	status: "in_progress",
	created_at: naive(READ_AT - 4 * DAY),
	first_claimed_at: naive(READ_AT - 2 * DAY),
	claimed_at: naive(READ_AT - 2 * HOUR),
	assignee: "someone@mit.edu#abc",
	lease_expired: false,
});
const neverClaimed = task({
	slug: "tk-never-claimed",
	status: "open",
	created_at: naive(READ_AT - 30 * DAY),
});

describe("taskBar", () => {
	it("draws a closed task from filing to close, with work from first claim", () => {
		const drawn = bar(closed);
		expect(drawn.lead).toEqual({
			start: READ_AT - 5 * DAY,
			end: READ_AT - 1 * DAY,
		});
		expect(drawn.work).toEqual({
			start: READ_AT - 3 * DAY,
			end: READ_AT - 1 * DAY,
		});
		expect(drawn.lease).toBeNull();
	});

	it("runs an in-flight task to the read and marks its current lease", () => {
		const drawn = bar(inFlight);
		expect(drawn.lead.end).toBe(READ_AT);
		expect(drawn.work).toEqual({ start: READ_AT - 2 * DAY, end: READ_AT });
		// The lease, not the work start: `claimed_at` moves on every renewal.
		expect(drawn.lease).toBe(READ_AT - 2 * HOUR);
	});

	it("falls back to updated_at for a lease with no claimed_at", () => {
		// The same fallback `readiness.status_pickable` uses, so the mark and
		// the server's `lease_expired` measure from one instant.
		const legacy = task({
			status: "in_progress",
			created_at: naive(READ_AT - DAY),
			updated_at: naive(READ_AT - 3 * HOUR),
		});
		expect(bar(legacy).lease).toBe(READ_AT - 3 * HOUR);
	});

	it("leaves out an open task nobody ever claimed", () => {
		expect(taskBar(neverClaimed, READ_AT)).toBe("backlog");
		expect(taskBar({ ...neverClaimed, status: "blocked" }, READ_AT)).toBe(
			"backlog",
		);
	});

	it("draws an open task that was claimed and released", () => {
		const released = task({
			created_at: naive(READ_AT - 3 * DAY),
			first_claimed_at: naive(READ_AT - 2 * DAY),
		});
		const drawn = bar(released);
		// When it was released is not recorded, so no segment runs to now.
		expect(drawn.work).toBeNull();
		expect(drawn.firstClaim).toBe(READ_AT - 2 * DAY);
		expect(drawn.lease).toBeNull();
	});

	it("has no work segment where the first claim was never recorded", () => {
		expect(bar({ ...closed, first_claimed_at: null }).work).toBeNull();
	});

	it("will not end a closed bar without a close time", () => {
		expect(taskBar({ ...closed, closed_at: null }, READ_AT)).toBe("undated");
	});
});

describe("packLanes", () => {
	const span = (slug: string, start: number, end: number): SessionSpan => ({
		session: session({ slug }),
		span: { start, end },
		open: false,
	});

	it("stacks overlapping sessions and reuses a lane once it frees up", () => {
		const lanes = packLanes([
			span("ws-c", 5, 8),
			span("ws-a", 0, 4),
			span("ws-b", 2, 6),
		]);
		expect(lanes.map((lane) => lane.map((s) => s.session.slug))).toEqual([
			["ws-a", "ws-c"],
			["ws-b"],
		]);
	});
});

describe("layout", () => {
	it("windows tasks by where their bar ends", () => {
		const old = {
			...closed,
			slug: "tk-old",
			created_at: naive(READ_AT - 40 * DAY),
			closed_at: naive(READ_AT - 20 * DAY),
		};
		const plan = layout(
			data({ tasks: [closed, inFlight, neverClaimed, old] }),
			DEFAULT_ROUTE,
		);
		const drawn = plan.groups.flatMap((group) =>
			group.bars.map((b) => b.task.slug),
		);
		// Sorted by start: the in-flight task was filed after the closed one.
		expect(drawn).toEqual(["tk-closed", "tk-in-flight"]);
		expect(plan.backlog).toBe(1);
		expect(plan.window).toEqual({ start: READ_AT - 14 * DAY, end: READ_AT });
	});

	it("counts closed tasks with no close time", () => {
		const plan = layout(
			data({ tasks: [{ ...closed, closed_at: null }] }),
			DEFAULT_ROUTE,
		);
		expect(plan.undated).toBe(1);
		expect(plan.groups).toEqual([]);
	});

	it("groups by project, titled from the project list, no project last", () => {
		const project = projects[0];
		if (!project) {
			throw new Error("workflow_project_list fixture is empty");
		}
		const plan = layout(
			data({
				tasks: [
					{ ...closed, project_slug: null },
					{ ...inFlight, project_slug: project.slug },
					{ ...closed, slug: "tk-z", project_slug: "wp-zzz-gone" },
				],
			}),
			DEFAULT_ROUTE,
		);
		expect(plan.groups.map((group) => group.title)).toEqual(
			[project.title, "wp-zzz-gone"]
				.sort((a, b) => a.localeCompare(b))
				.concat("No project"),
		);
	});

	it("keeps sessions in the window, an open one as its start alone", () => {
		const plan = layout(
			data({
				sessions: [
					session({
						slug: "ws-ended",
						started_at: iso(READ_AT - 2 * DAY),
						ended_at: iso(READ_AT - 2 * DAY + 3 * HOUR),
					}),
					session({
						slug: "ws-open",
						started_at: iso(READ_AT - DAY),
						ended_at: null,
					}),
					session({
						slug: "ws-before",
						started_at: iso(READ_AT - 30 * DAY),
						ended_at: iso(READ_AT - 29 * DAY),
					}),
					// Never ended, and started before the window: most likely
					// abandoned, and a bar to now would claim a month of work.
					session({
						slug: "ws-abandoned",
						started_at: iso(READ_AT - 30 * DAY),
						ended_at: null,
					}),
				],
			}),
			DEFAULT_ROUTE,
		);
		const spans = plan.groups.flatMap((group) => group.lanes.flat());
		expect(spans.map((s) => [s.session.slug, s.span.end, s.open])).toEqual([
			["ws-ended", READ_AT - 2 * DAY + 3 * HOUR, false],
			["ws-open", READ_AT - DAY, true],
		]);
	});

	it("narrows sessions to projects that name the filtered repo", () => {
		const project = projects.find((p) => p.repos?.includes(AGENT_KIT));
		if (!project) {
			throw new Error("no fixture project names agent-kit");
		}
		const recent = {
			started_at: iso(READ_AT - DAY),
			ended_at: iso(READ_AT - DAY + HOUR),
		};
		const all = [
			session({ ...recent, slug: "ws-in", project_slug: project.slug }),
			session({ ...recent, slug: "ws-none", project_slug: null }),
		];
		const slugsFor = (route: Route) =>
			layout(data({ sessions: all }), route).groups.flatMap((group) =>
				group.lanes.flat().map((s) => s.session.slug),
			);

		expect(slugsFor({ ...DEFAULT_ROUTE, repo: AGENT_KIT })).toEqual(["ws-in"]);
		expect(slugsFor({ ...DEFAULT_ROUTE, repo: HQ })).toEqual([]);
		expect(slugsFor(DEFAULT_ROUTE).sort()).toEqual(["ws-in", "ws-none"]);
	});

	it("keeps the sessions and title of a project that is no longer active", () => {
		// The shell's project list is active-only; a retrospective is mostly
		// about work that finished.
		const done: WorkflowProjectSummary = {
			...(projects[0] as WorkflowProjectSummary),
			slug: "wp-done",
			title: "Finished last week",
			status: "completed",
			repos: [AGENT_KIT],
		};
		const plan = layout(
			data({
				projects: [...projects, done],
				sessions: [
					session({
						slug: "ws-done",
						project_slug: "wp-done",
						started_at: iso(READ_AT - 5 * DAY),
						ended_at: iso(READ_AT - 5 * DAY + HOUR),
					}),
				],
			}),
			{ ...DEFAULT_ROUTE, repo: AGENT_KIT },
		);
		expect(plan.groups.map((group) => group.title)).toEqual([
			"Finished last week",
		]);
		expect(plan.groups[0]?.lanes.flat().map((s) => s.session.slug)).toEqual([
			"ws-done",
		]);
	});

	it("narrows tasks by the board's scope rule", () => {
		const plan = layout(data({ tasks: [{ ...closed, repo: HQ }, inFlight] }), {
			...DEFAULT_ROUTE,
			repo: AGENT_KIT,
		});
		expect(
			plan.groups.flatMap((group) => group.bars.map((b) => b.task.slug)),
		).toEqual(["tk-in-flight"]);
	});
});

describe("ticks", () => {
	it("puts one on each UTC midnight in a short window", () => {
		const marks = ticks({ start: READ_AT - 7 * DAY, end: READ_AT }, 7);
		expect(marks).toHaveLength(7);
		expect(marks.every((at) => at % DAY === 0)).toBe(true);
	});

	it("thins them out on a long one", () => {
		expect(
			ticks({ start: READ_AT - 90 * DAY, end: READ_AT }, 90).length,
		).toBeLessThanOrEqual(13);
	});
});

describe("timeline", () => {
	let root: HTMLElement;

	beforeEach(() => {
		root = document.createElement("div");
		document.body.replaceChildren(root);
	});

	/** The rendered text with template whitespace collapsed. */
	function text(): string {
		return (root.textContent ?? "").replace(/\s+/g, " ");
	}

	function draw(value: Timeline, route: Route = DEFAULT_ROUTE): void {
		render(timeline(value, route), root);
	}

	function row(slug: string): Element {
		const found = root.querySelector(`.tl-row[data-slug="${slug}"]`);
		if (!found) {
			throw new Error(`no row for ${slug}`);
		}
		return found;
	}

	/** A rect's `x` and `width`, which are percentages of the track. */
	function x(rect: Element | null): [number, number] {
		const read = (name: string) =>
			Number.parseFloat(rect?.getAttribute(name) ?? "");
		return [read("x"), read("width")];
	}

	it("places bars where their timestamps are", () => {
		draw(data({ tasks: [closed, inFlight] }));

		// A 14-day window across 100%: one day is 100/14.
		const unit = 100 / 14;
		const [leadX, leadW] = x(row("tk-closed").querySelector("rect.lead"));
		expect(leadX).toBeCloseTo(9 * unit);
		expect(leadW).toBeCloseTo(4 * unit);
		const [workX, workW] = x(row("tk-closed").querySelector("rect.work"));
		expect(workX).toBeCloseTo(11 * unit);
		expect(workW).toBeCloseTo(2 * unit);

		const [flightX, flightW] = x(
			row("tk-in-flight").querySelector("rect.lead"),
		);
		expect(flightX + flightW).toBeCloseTo(100);
		const lease = row("tk-in-flight").querySelector("line.lease");
		expect(Number.parseFloat(lease?.getAttribute("x1") ?? "")).toBeCloseTo(
			100 - (2 / 24) * unit,
		);
	});

	it("clips a bar that starts before the window", () => {
		const long = {
			...closed,
			created_at: naive(READ_AT - 60 * DAY),
		};
		draw(data({ tasks: [long] }));
		expect(x(row("tk-closed").querySelector("rect.lead"))[0]).toBe(0);
	});

	it("hatches a bar with no first-claim time", () => {
		draw(data({ tasks: [{ ...closed, first_claimed_at: null }] }));
		const lead = row("tk-closed").querySelector("rect.lead");
		expect(lead?.classList.contains("hatched")).toBe(true);
		expect(row("tk-closed").querySelector("rect.work")).toBeNull();
	});

	it("does not pin a lease older than the window to its left edge", () => {
		draw(
			data({
				tasks: [
					{
						...inFlight,
						claimed_at: naive(READ_AT - 20 * DAY),
						lease_expired: true,
					},
				],
			}),
		);
		expect(row("tk-in-flight").querySelector("line.lease")).toBeNull();
		// Still named, on the bar.
		expect(
			row("tk-in-flight").querySelector("rect.lead")?.textContent,
		).toContain("Current lease since");
	});

	it("marks a released task's first claim instead of a segment to now", () => {
		draw(
			data({
				tasks: [
					task({
						slug: "tk-released",
						created_at: naive(READ_AT - 3 * DAY),
						first_claimed_at: naive(READ_AT - 2 * DAY),
					}),
				],
			}),
		);
		expect(row("tk-released").querySelector("rect.work")).toBeNull();
		const lead = row("tk-released").querySelector("rect.lead");
		expect(lead?.classList.contains("hatched")).toBe(false);
		const first = row("tk-released").querySelector("line.first-claim");
		expect(Number.parseFloat(first?.getAttribute("x1") ?? "")).toBeCloseTo(
			(12 / 14) * 100,
		);
	});

	it("does not pin a first claim older than the window to its left edge", () => {
		draw(
			data({
				tasks: [
					task({
						slug: "tk-released",
						created_at: naive(READ_AT - 40 * DAY),
						first_claimed_at: naive(READ_AT - 30 * DAY),
					}),
				],
			}),
		);
		expect(row("tk-released").querySelector("line.first-claim")).toBeNull();
		expect(
			row("tk-released").querySelector("rect.lead")?.textContent,
		).toContain("The release time is not recorded");
	});

	it("marks a lapsed lease", () => {
		draw(data({ tasks: [{ ...inFlight, lease_expired: true }] }));
		const lease = row("tk-in-flight").querySelector("line.lease");
		expect(lease?.classList.contains("stale")).toBe(true);
		expect(lease?.textContent).toContain("lapsed");
	});

	it("says how many never-claimed tasks it left out", () => {
		draw(data({ tasks: [closed, neverClaimed] }));
		expect(text()).toContain("1 open task never claimed is not drawn");
	});

	it("says so when nothing happened in the window", () => {
		draw(data({ tasks: [neverClaimed] }));
		expect(text()).toContain("Nothing was worked in the last 14 days");
	});

	it("warns when the task read hit its limit", () => {
		draw(data({ tasks: [closed], truncated: true }));
		expect(text()).toContain("row limit");
	});

	it("links each task into the detail panel", () => {
		draw(data({ tasks: [closed] }), { ...DEFAULT_ROUTE, view: "timeline" });
		expect(
			row("tk-closed").querySelector("a.tl-label")?.getAttribute("href"),
		).toBe("#timeline?slug=tk-closed");
	});

	it("offers each window and marks the current one", () => {
		draw(data({ days: 30 }), { ...DEFAULT_ROUTE, view: "timeline", days: 30 });
		const current = root.querySelector('.window a[aria-current="page"]');
		expect(current?.textContent?.trim()).toBe("30d");
	});

	it("draws an open session as a start mark, not a bar", () => {
		draw(
			data({
				tasks: [closed],
				sessions: [session({ started_at: iso(READ_AT - DAY), ended_at: null })],
			}),
		);
		expect(root.querySelector(".tl-sessions rect.session")).toBeNull();
		const mark = root.querySelector(".tl-sessions line.session-start");
		expect(Number.parseFloat(mark?.getAttribute("x1") ?? "")).toBeCloseTo(
			(13 / 14) * 100,
		);
		expect(mark?.textContent).toContain("never ended");
	});

	it("positions nothing with an inline style, which the CSP blocks", () => {
		// `/ui/` is served with `default-src 'self'`, so a `style=` attribute is
		// dropped and a bar positioned by one would render at zero width.
		draw(
			data({
				tasks: [closed, inFlight],
				sessions: [session({ started_at: iso(READ_AT - DAY), ended_at: null })],
			}),
		);
		expect(root.querySelector("[style]")).toBeNull();
		// And reaches nothing off-origin: no script, image or external link.
		expect(
			root.querySelector("script, img, link, use[href^='http']"),
		).toBeNull();
	});
});
