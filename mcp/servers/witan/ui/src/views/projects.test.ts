import { render } from "lit-html";
import { beforeEach, describe, expect, it } from "vitest";
import taskListFixture from "../../fixtures/task_list.json" with {
	type: "json",
};
import projectGetFixture from "../../fixtures/workflow_project_get.json" with {
	type: "json",
};
import projectListFixture from "../../fixtures/workflow_project_list.json" with {
	type: "json",
};
import projectStatusFixture from "../../fixtures/workflow_project_status.json" with {
	type: "json",
};
import sessionListFixture from "../../fixtures/workflow_session_list.json" with {
	type: "json",
};
import { DEFAULT_ROUTE, type Route } from "../route.js";
import type {
	ProjectStatus,
	TaskRow,
	WorkflowProjectDetail,
	WorkflowProjectSummary,
	WorkflowSession,
} from "../types.js";
import { unwrap } from "../unwrap.js";
import {
	projectList,
	projectRollup,
	projectsWithAssignee,
	type Rollup,
} from "./projects.js";

/**
 * The rollup and the list, rendered from REAL recorded tool results.
 *
 * Same contract as `fixtures.test.ts`: a server change that renames a field
 * these views read fails here, in the PR that renamed it, rather than showing
 * up as a blank column on a deployed page.
 */

const projects = unwrap<WorkflowProjectSummary[]>(
	"workflow_project_list",
	projectListFixture,
);
const detail = unwrap<WorkflowProjectDetail>(
	"workflow_project_get",
	projectGetFixture,
);
const status = unwrap<ProjectStatus>(
	"workflow_project_status",
	projectStatusFixture,
);
const tasks = unwrap<TaskRow[]>("task_list", taskListFixture);
const sessions = unwrap<WorkflowSession[]>(
	"workflow_session_list",
	sessionListFixture,
);

const rollup: Rollup = { detail, status, tasks, sessions };

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement("div");
	document.body.replaceChildren(root);
});

function text(): string {
	return root.textContent ?? "";
}

describe("projectList", () => {
	it("links each project into its rollup", () => {
		render(projectList(projects, DEFAULT_ROUTE), root);

		const link = root.querySelector<HTMLAnchorElement>("tbody a");
		expect(link?.textContent?.trim()).toBe(detail.title);
		expect(link?.getAttribute("href")).toBe(`#projects?project=${detail.slug}`);
	});

	it("shortens the repo URIs", () => {
		render(projectList(projects, DEFAULT_ROUTE), root);
		expect(text()).toContain("mitodl/agent-kit");
	});

	it("says which scope is empty rather than reporting an error", () => {
		// An empty result and a failed read are different situations, and the
		// CLI confusing the two is what put this on the task.
		const scoped: Route = {
			...DEFAULT_ROUTE,
			repo: "https://github.com/mitodl/hq",
		};
		render(projectList([], scoped), root);
		expect(text()).toContain("No projects in mitodl/hq");
	});
});

describe("projectRollup", () => {
	it("draws the detail projection's own fields", () => {
		render(projectRollup(rollup, DEFAULT_ROUTE), root);

		// `description` and `author` exist only on `workflow_project_get`; the
		// rollup's six-field `project` has neither. Reading them here is what
		// stops the two projections being folded back into one type.
		expect(text()).toContain("A human-readable surface for the graph.");
		expect(text()).toContain("fixtures");
		expect(text()).toContain("discovery");
	});

	it("draws ready work from the server's own rule", () => {
		render(projectRollup(rollup, DEFAULT_ROUTE), root);

		const ready = root.querySelectorAll(".ready li");
		expect(ready.length).toBe(status.ready_tasks.length);
		expect(ready[0]?.textContent).toContain(status.ready_tasks[0]?.title);
	});

	it("links a ready task into the detail panel", () => {
		render(projectRollup(rollup, DEFAULT_ROUTE), root);

		const link = root.querySelector<HTMLAnchorElement>(".ready a");
		expect(link?.getAttribute("href")).toContain(
			`slug=${status.ready_tasks[0]?.slug}`,
		);
	});

	it("elides closed tasks until they are asked for", () => {
		const closed: TaskRow[] = [
			...tasks,
			{
				...(tasks[0] as TaskRow),
				slug: "tk-done",
				title: "Finished",
				status: "closed",
			},
		];
		render(projectRollup({ ...rollup, tasks: closed }, DEFAULT_ROUTE), root);
		expect(text()).not.toContain("Finished");

		render(
			projectRollup(
				{ ...rollup, tasks: closed },
				{ ...DEFAULT_ROUTE, closed: true },
			),
			root,
		);
		expect(text()).toContain("Finished");
	});

	it("marks a lapsed claim on the task table", () => {
		const lapsed: TaskRow[] = [
			{ ...(tasks[0] as TaskRow), lease_expired: true },
			...tasks.slice(1),
		];
		render(projectRollup({ ...rollup, tasks: lapsed }, DEFAULT_ROUTE), root);
		expect(root.querySelector(".stale-claim")).not.toBeNull();
	});

	it("shows the session summaries", () => {
		render(projectRollup(rollup, DEFAULT_ROUTE), root);
		expect(text()).toContain("Recorded the fixtures.");
	});

	it("puts the newest session first", () => {
		// `workflow_session_list` returns them ascending, and the CURRENT handoff
		// summary is what a person opens a rollup to read. Unreversed, it sits at
		// the bottom of a growing list.
		const first = sessions[0] as WorkflowSession;
		const ordered: WorkflowSession[] = [
			{ ...first, slug: "ws-old", summary: "the older one" },
			{ ...first, slug: "ws-new", summary: "the newer one" },
		];
		render(
			projectRollup({ ...rollup, sessions: ordered }, DEFAULT_ROUTE),
			root,
		);

		const summaries = [...root.querySelectorAll(".sessions .summary")].map(
			(p) => p.textContent,
		);
		expect(summaries).toEqual(["the newer one", "the older one"]);
	});

	it("reports the open count beside the row count, not as a denominator", () => {
		// "N of M open" read as a truncation indicator: the numbers are equal
		// with Closed unticked and the first is the larger with it ticked.
		render(projectRollup(rollup, DEFAULT_ROUTE), root);

		const heading = [...root.querySelectorAll("h3")].find((h) =>
			h.textContent?.includes("Tasks"),
		);
		expect(heading?.textContent).not.toMatch(/\d+\s+of\s+\d+/);
		expect(heading?.textContent).toContain(`${status.counts.open_tasks} open`);
	});

	it("drops the project when a repo link inside the rollup is followed", () => {
		// ★ `workflow_project_status` takes no `repo`, so its counts and Ready
		// list stay project-wide. Keeping the project here would narrow only the
		// task table, and the header would then disagree with the rows under it.
		render(projectRollup(rollup, DEFAULT_ROUTE), root);

		const repoLink = [
			...root.querySelectorAll<HTMLAnchorElement>(".facts a"),
		].find((a) => a.textContent?.includes("mitodl/agent-kit"));
		expect(repoLink?.getAttribute("href")).toBe(
			"#projects?repo=https%3A%2F%2Fgithub.com%2Fmitodl%2Fagent-kit",
		);
	});

	it("says a stale project slug is missing, not broken", () => {
		const route: Route = { ...DEFAULT_ROUTE, project: "wp-gone" };
		render(
			projectRollup(
				{ detail: null, status: null, tasks: [], sessions: [] },
				route,
			),
			root,
		);
		expect(text()).toContain("No project");
		expect(text()).toContain("wp-gone");
	});

	it("renders no dependency section when the project has no edges", () => {
		// The fixture project has no `blocked_by`, no `blocks` and no blockers,
		// which is the common case; empty scaffolding for it is what the task's
		// acceptance criterion rules out.
		render(projectRollup(rollup, DEFAULT_ROUTE), root);
		expect(text()).not.toContain("Dependencies");
	});

	it("draws the dependency section when there are edges", () => {
		const blocked: WorkflowProjectDetail = {
			...detail,
			blocked_by: ["wp-other"],
			blocks: ["wp-later"],
		};
		render(projectRollup({ ...rollup, detail: blocked }, DEFAULT_ROUTE), root);

		expect(text()).toContain("Dependencies");
		const links = [...root.querySelectorAll<HTMLAnchorElement>(".edges a")].map(
			(a) => a.getAttribute("href"),
		);
		expect(links).toContain("#projects?project=wp-other");
		expect(links).toContain("#projects?project=wp-later");
	});
});

describe("projectRollup with an assignee filter", () => {
	const ME = "dfrapp@mit.edu";
	const mine = {
		...(tasks[0] as TaskRow),
		slug: "tk-mine",
		assignee: `${ME}#1`,
	};
	const nobody = {
		...(tasks[0] as TaskRow),
		slug: "tk-nobody",
		assignee: null,
	};
	const filtered = { ...rollup, tasks: [mine, nobody] };

	function rows(assignees: string[]): string[] {
		render(projectRollup(filtered, { ...DEFAULT_ROUTE, assignees }), root);
		return [...root.querySelectorAll("table a[href*='slug=']")].map(
			(link) =>
				new URLSearchParams((link as HTMLAnchorElement).hash.split("?")[1]).get(
					"slug",
				) as string,
		);
	}

	it("narrows the task table to the person", () => {
		expect(rows([ME])).toEqual(["tk-mine"]);
	});

	it("narrows it to unassigned tasks", () => {
		expect(rows(["unassigned"])).toEqual(["tk-nobody"]);
	});

	it("narrows the Ready list to the selection", () => {
		const held = {
			...status,
			ready_tasks: status.ready_tasks.map((task) => ({
				...task,
				assignee: "ada@mit.edu",
			})),
		};
		render(
			projectRollup(
				{ ...rollup, status: held },
				{ ...DEFAULT_ROUTE, assignees: [ME] },
			),
			root,
		);

		expect(root.querySelectorAll(".ready li")).toHaveLength(0);
	});
});

describe("projectsWithAssignee", () => {
	const ME = "dfrapp@mit.edu";
	const [first, second] = projects as [
		WorkflowProjectSummary,
		WorkflowProjectSummary,
	];
	const other = { ...first, slug: "wp-other" };
	const third = { ...second, slug: "wp-third" };
	const all = [first, other, third];
	const base = tasks[0] as TaskRow;
	const held = (project: string, assignee: string | null) => ({
		...base,
		project_slug: project,
		assignee,
	});
	const slugs = (list: WorkflowProjectSummary[]) => list.map((p) => p.slug);

	it("keeps every project with no selection", () => {
		expect(projectsWithAssignee(all, [], [])).toBe(all);
	});

	it("keeps only projects with a task held by the person, whatever the session", () => {
		const list = projectsWithAssignee(
			all,
			[held(first.slug, `${ME}#abc`), held("wp-other", "ada@mit.edu")],
			[ME],
		);

		expect(slugs(list)).toEqual([first.slug]);
	});

	it("keeps projects with an unassigned task for Unassigned", () => {
		const list = projectsWithAssignee(
			all,
			[held(first.slug, ME), held("wp-other", null)],
			["unassigned"],
		);

		expect(slugs(list)).toEqual(["wp-other"]);
	});

	it("is the union of the people selected", () => {
		const list = projectsWithAssignee(
			all,
			[held(first.slug, ME), held("wp-third", "ada@mit.edu")],
			[ME, "ada@mit.edu"],
		);

		expect(slugs(list)).toEqual([first.slug, "wp-third"]);
	});

	it("ignores tasks that belong to no project", () => {
		expect(projectsWithAssignee(all, [held("", ME)], [ME])).toEqual([]);
	});
});

describe("projectList with an assignee filter", () => {
	it("says so when no project matches", () => {
		render(
			projectList(
				projects,
				{ ...DEFAULT_ROUTE, assignees: ["nobody@x.edu"] },
				[],
			),
			root,
		);

		expect(text()).toContain("No projects have a task held by");
	});
});
