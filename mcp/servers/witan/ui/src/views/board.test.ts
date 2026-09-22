import { render } from "lit-html";
import { beforeEach, describe, expect, it } from "vitest";
import taskListFixture from "../../fixtures/task_list.json" with {
	type: "json",
};
import taskReadyFixture from "../../fixtures/task_ready.json" with {
	type: "json",
};
import { DEFAULT_ROUTE, type Route } from "../route.js";
import type { TaskRow } from "../types.js";
import { unwrap } from "../unwrap.js";
import {
	type Board,
	board,
	CLOSED_SHOWN,
	columns,
	inScope,
	leaseStart,
} from "./board.js";

/**
 * The board, rendered from the REAL recorded `task_ready` and `task_list`
 * results where the fixtures carry the case, and from rows derived from them
 * where they do not (a lapsed lease, a closed task, another repo). Deriving
 * rather than writing rows from scratch keeps every field the server sends,
 * so a renamed field still fails here.
 */

const ready = unwrap<TaskRow[]>("task_ready", taskReadyFixture);
const tasks = unwrap<TaskRow[]>("task_list", taskListFixture);
const byStatus = (status: TaskRow["status"]) =>
	tasks.filter((task) => task.status === status);

const AGENT_KIT = "https://github.com/mitodl/agent-kit";
const HQ = "https://github.com/mitodl/hq";
const NOW = Date.parse("2026-01-01T06:00:00Z");

function row(patch: Partial<TaskRow>): TaskRow {
	const base = tasks[0];
	if (!base) {
		throw new Error("task_list fixture is empty");
	}
	return { ...base, ...patch };
}

function fromFixtures(patch: Partial<Board> = {}): Board {
	return {
		ready,
		live: tasks.filter((task) => task.status !== "closed"),
		closed: null,
		truncated: false,
		...patch,
	};
}

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement("div");
	document.body.replaceChildren(root);
});

function draw(data: Board, route: Route = DEFAULT_ROUTE): void {
	render(board(data, route, NOW), root);
}

function column(name: string): HTMLElement {
	const section = root.querySelector<HTMLElement>(
		`section.column[aria-label="${name}"]`,
	);
	if (!section) {
		throw new Error(`no ${name} column`);
	}
	return section;
}

function slugsIn(name: string): string[] {
	return [...column(name).querySelectorAll<HTMLAnchorElement>(".card-head a")]
		.map((link) => new URLSearchParams(link.hash.split("?")[1]).get("slug"))
		.filter((slug): slug is string => slug !== null);
}

describe("the Ready column", () => {
	it("is task_ready's result, in task_ready's order", () => {
		draw(fromFixtures());

		expect(slugsIn("Ready")).toEqual(ready.map((task) => task.slug));
	});

	it("keeps a blocked-status task that task_ready calls ready out of Blocked", () => {
		// `task_ready` counts `blocked` as pickable once its blockers close; the
		// board follows the tool rather than the status word.
		const stale = row({ slug: "tk-was-blocked", status: "blocked" });
		draw(
			fromFixtures({
				ready: [...ready, stale],
				live: [...tasks, stale],
			}),
		);

		expect(slugsIn("Ready")).toContain("tk-was-blocked");
		expect(slugsIn("Blocked")).not.toContain("tk-was-blocked");
	});
});

describe("the In progress column", () => {
	it("shows who holds each task and since when", () => {
		draw(fromFixtures());

		const [held] = byStatus("in_progress");
		expect(slugsIn("In progress")).toEqual([held?.slug]);
		const claim = column("In progress").querySelector(".claim");
		expect(claim?.textContent).toContain(held?.assignee ?? "");
		expect(claim?.textContent).toContain("claimed 6h ago");
	});

	it("marks a lapsed claim exactly when the server says so", () => {
		const lapsed = row({
			slug: "tk-lapsed",
			status: "in_progress",
			assignee: "gone-agent",
			lease_expired: true,
		});
		draw(fromFixtures({ live: [...tasks, lapsed] }));

		const cards = column("In progress").querySelectorAll("li.card");
		const marked = [...cards].filter((card) =>
			card.classList.contains("stale"),
		);
		expect(marked).toHaveLength(1);
		expect(marked[0]?.textContent).toContain("claim lapsed");
		expect(marked[0]?.textContent).toContain("gone-agent");
	});

	it("marks a lapsed claim that also has an open blocker", () => {
		// It never appears in Ready (a blocker is open), so a board that looked
		// for stale claims only there would miss it.
		const [blocker] = byStatus("open");
		const lapsed = row({
			slug: "tk-lapsed-blocked",
			status: "in_progress",
			lease_expired: true,
			blocked_by: [blocker?.slug ?? ""],
		});
		draw(fromFixtures({ live: [...tasks, lapsed] }));

		const card = column("In progress").querySelector("li.card.stale");
		expect(card?.textContent).toContain("claim lapsed");
		expect(card?.textContent).toContain(blocker?.title ?? "");
	});

	it("measures a legacy claim with no claimed_at from updated_at", () => {
		const legacy = row({
			status: "in_progress",
			claimed_at: null,
			updated_at: "2026-01-01T04:00:00",
		});

		expect(leaseStart(legacy)).toBe("2026-01-01T04:00:00");
		draw(fromFixtures({ ready: [], live: [legacy] }));
		expect(column("In progress").textContent).toContain("claimed 2h ago");
	});

	it("puts the longest-held claim first", () => {
		const older = row({
			slug: "tk-older",
			status: "in_progress",
			claimed_at: "2026-01-01T01:00:00",
		});
		const newer = row({
			slug: "tk-newer",
			status: "in_progress",
			claimed_at: "2026-01-01T05:00:00",
		});

		draw(fromFixtures({ ready: [], live: [newer, older] }));

		expect(slugsIn("In progress")).toEqual(["tk-older", "tk-newer"]);
	});
});

describe("the Blocked column", () => {
	it("names and links each open blocker", () => {
		draw(fromFixtures());

		const [blocked] = byStatus("blocked");
		const [blockerSlug] = blocked?.blocked_by ?? [];
		const blocker = tasks.find((task) => task.slug === blockerSlug);
		expect(slugsIn("Blocked")).toEqual([blocked?.slug]);

		const link =
			column("Blocked").querySelector<HTMLAnchorElement>("ul.blockers a");
		expect(link?.textContent).toBe(blocker?.title);
		expect(link?.getAttribute("href")).toBe(`#projects?slug=${blockerSlug}`);
		expect(
			column("Blocked").querySelector("ul.blockers")?.textContent,
		).toContain("open");
	});

	it("names a blocker from another repo that the scope leaves out", () => {
		// ★ The reason the live set is read across all repos: scoped to one repo,
		// a cross-repo blocker is indistinguishable from a closed one.
		const elsewhere = row({
			slug: "tk-elsewhere",
			title: "Keycloak client",
			repo: HQ,
			status: "open",
		});
		const waiting = row({
			slug: "tk-waiting",
			status: "open",
			blocked_by: ["tk-elsewhere"],
		});
		draw(fromFixtures({ ready: [], live: [waiting, elsewhere] }), {
			...DEFAULT_ROUTE,
			repo: AGENT_KIT,
		});

		expect(slugsIn("Blocked")).toEqual(["tk-waiting"]);
		expect(column("Blocked").textContent).toContain("Keycloak client");
	});

	it("does not list a blocker that has closed", () => {
		const [open] = byStatus("open");
		const waiting = row({
			slug: "tk-waiting",
			status: "open",
			// `tk-closed` is in no live read, which is what "closed" looks like.
			blocked_by: ["tk-closed", open?.slug ?? ""],
		});
		draw(fromFixtures({ ready: [], live: [waiting, ...tasks] }));

		const blockers = column("Blocked").querySelectorAll("ul.blockers li");
		const texts = [...blockers].map((li) => li.textContent ?? "");
		expect(texts.join(" ")).not.toContain("tk-closed");
		expect(texts.join(" ")).toContain(open?.title ?? "");
	});

	it("says so when a blocked task has no blocker linked", () => {
		const bare = row({ slug: "tk-bare", status: "blocked", blocked_by: null });
		draw(fromFixtures({ ready: [], live: [bare] }));

		expect(column("Blocked").textContent).toContain(
			"Marked blocked, with no blocker linked.",
		);
	});
});

describe("the Closed column", () => {
	it("is absent unless the Closed filter asked for it", () => {
		draw(fromFixtures());

		expect(
			root.querySelector('section.column[aria-label="Closed"]'),
		).toBeNull();
	});

	it("puts the newest closed_at first and caps what it draws", () => {
		const closed = Array.from({ length: CLOSED_SHOWN + 5 }, (_, index) =>
			row({
				slug: `tk-closed-${index}`,
				status: "closed",
				closed_at: `2026-01-01T00:${String(index).padStart(2, "0")}:00`,
			}),
		);
		draw(fromFixtures({ closed }));

		const shown = slugsIn("Closed");
		expect(shown).toHaveLength(CLOSED_SHOWN);
		expect(shown[0]).toBe(`tk-closed-${CLOSED_SHOWN + 4}`);
		expect(column("Closed").querySelector("h3")?.textContent).toContain(
			`newest ${CLOSED_SHOWN} of ${CLOSED_SHOWN + 5}`,
		);
	});
});

describe("scope", () => {
	const mine = row({ slug: "tk-mine", repo: AGENT_KIT, project_slug: "wp-a" });
	const unscoped = row({ slug: "tk-unscoped", repo: null, project_slug: null });
	const theirs = row({ slug: "tk-theirs", repo: HQ, project_slug: "wp-b" });

	it("keeps a repo's own tasks and the unscoped ones, as the tools do", () => {
		const route = { ...DEFAULT_ROUTE, repo: AGENT_KIT };

		expect(inScope(mine, route)).toBe(true);
		expect(inScope(unscoped, route)).toBe(true);
		expect(inScope(theirs, route)).toBe(false);
	});

	it("lets a project override the repo", () => {
		const route = { ...DEFAULT_ROUTE, repo: AGENT_KIT, project: "wp-b" };

		expect(inScope(mine, route)).toBe(false);
		expect(inScope(theirs, route)).toBe(true);
	});

	it("narrows every column but Ready, which the server already scoped", () => {
		const route = { ...DEFAULT_ROUTE, repo: HQ };
		const cols = columns(
			fromFixtures({
				live: [
					...tasks,
					row({ slug: "tk-hq", repo: HQ, status: "in_progress" }),
				],
			}),
			route,
		);

		expect(cols.ready).toBe(ready);
		expect(cols.inProgress.map((task) => task.slug)).toEqual(["tk-hq"]);
		expect(cols.blocked).toEqual([]);
	});
});

it("warns when a read came back at its limit", () => {
	draw(fromFixtures({ truncated: true }));

	expect(root.querySelector(".note")?.textContent).toContain("row limit");
});
