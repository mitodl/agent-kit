import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import taskGetFixture from "../fixtures/task_get.json" with { type: "json" };
import taskListFixture from "../fixtures/task_list.json" with { type: "json" };
import projectGetFixture from "../fixtures/workflow_project_get.json" with {
	type: "json",
};
import projectListFixture from "../fixtures/workflow_project_list.json" with {
	type: "json",
};
import projectStatusFixture from "../fixtures/workflow_project_status.json" with {
	type: "json",
};
import sessionListFixture from "../fixtures/workflow_session_list.json" with {
	type: "json",
};
import type {
	ProjectStatus,
	TaskDetail,
	TaskRow,
	WorkflowProjectDetail,
	WorkflowProjectSummary,
	WorkflowSession,
} from "./types.js";
import { unwrap } from "./unwrap.js";

/**
 * The wiring, with the transport swapped out.
 *
 * The reads are stubbed but their RESULTS are the recorded fixtures, so this
 * exercises the one thing the view tests cannot: which tool the app calls for
 * a given route, and what it does when the route changes under an in-flight
 * read.
 */
vi.mock("./mcp.js", () => ({
	workflowProjectList: vi.fn(),
	workflowProjectGet: vi.fn(),
	workflowProjectStatus: vi.fn(),
	workflowSessionList: vi.fn(),
	taskList: vi.fn(),
	taskGet: vi.fn(),
}));

const mcp = await import("./mcp.js");
const { App, reposIn } = await import("./app.js");

const projects = unwrap<WorkflowProjectSummary[]>(
	"workflow_project_list",
	projectListFixture,
);
const projectDetail = unwrap<WorkflowProjectDetail>(
	"workflow_project_get",
	projectGetFixture,
);
const projectStatus = unwrap<ProjectStatus>(
	"workflow_project_status",
	projectStatusFixture,
);
const tasks = unwrap<TaskRow[]>("task_list", taskListFixture);
const sessions = unwrap<WorkflowSession[]>(
	"workflow_session_list",
	sessionListFixture,
);
const task = unwrap<TaskDetail>("task_get", taskGetFixture);

let root: HTMLElement;
let app: InstanceType<typeof App>;

beforeEach(() => {
	vi.mocked(mcp.workflowProjectList).mockResolvedValue(projects);
	vi.mocked(mcp.workflowProjectGet).mockResolvedValue(projectDetail);
	vi.mocked(mcp.workflowProjectStatus).mockResolvedValue(projectStatus);
	vi.mocked(mcp.workflowSessionList).mockResolvedValue(sessions);
	vi.mocked(mcp.taskList).mockResolvedValue(tasks);
	vi.mocked(mcp.taskGet).mockResolvedValue(task);

	window.location.hash = "";
	root = document.createElement("div");
	document.body.replaceChildren(root);
});

afterEach(() => {
	app?.stop();
	vi.clearAllMocks();
});

/** Start the app at a fragment and wait for its first reads to land. */
async function open(hash: string): Promise<void> {
	// Through `location`, not the constructor argument: the app navigates by
	// writing the fragment, so a test that started somewhere the URL does not
	// agree with would not be exercising the same thing.
	window.location.hash = hash;
	app = new App(root);
	app.start();
	await vi.waitFor(() => expect(root.querySelector("main")).not.toBeNull());
}

function text(): string {
	return root.textContent ?? "";
}

describe("App", () => {
	it("reads projects across all repos, whatever the filter says", async () => {
		// ★ The repo filter narrows in the browser. A read already scoped to one
		// repo would leave the filter offering only that repo, with no way back.
		await open("#projects?repo=https%3A%2F%2Fgithub.com%2Fmitodl%2Fhq");

		expect(mcp.workflowProjectList).toHaveBeenCalledWith({ repo: "" });
	});

	it("narrows the list to the filtered repo", async () => {
		await open("#projects?repo=https%3A%2F%2Fgithub.com%2Fmitodl%2Fhq");
		await vi.waitFor(() =>
			expect(text()).toContain("No projects in mitodl/hq"),
		);

		expect(text()).not.toContain(projectDetail.title);
	});

	it("lists every project when no repo is filtered", async () => {
		await open("");
		await vi.waitFor(() => expect(text()).toContain(projectDetail.title));
	});

	it("runs the four rollup reads for a selected project", async () => {
		await open(`#projects?project=${projectDetail.slug}`);
		await vi.waitFor(() => expect(text()).toContain("Sessions"));

		expect(mcp.workflowProjectGet).toHaveBeenCalledWith(projectDetail.slug);
		expect(mcp.workflowProjectStatus).toHaveBeenCalledWith(projectDetail.slug);
		expect(mcp.workflowSessionList).toHaveBeenCalledWith({
			project_slug: projectDetail.slug,
		});
		// An explicit limit: `task_list` silently caps an unscoped read at 50.
		expect(mcp.taskList).toHaveBeenCalledWith(
			expect.objectContaining({
				project_slug: projectDetail.slug,
				limit: 1000,
			}),
		);
	});

	it("opens the detail panel for the slug in the URL", async () => {
		await open("#projects?slug=tk-fixture-000");
		await vi.waitFor(() =>
			expect(root.querySelector(".detail-panel")).not.toBeNull(),
		);

		expect(mcp.taskGet).toHaveBeenCalledWith("tk-fixture-000");
		expect(root.querySelector(".detail-panel")?.textContent).toContain(
			task.title,
		);
	});

	it("moves focus into the panel when it opens", async () => {
		await open("#projects?slug=tk-fixture-000");
		await vi.waitFor(() =>
			expect(document.activeElement).toBe(root.querySelector(".detail-panel")),
		);
	});

	it("treats a null task as a stale link, not a failure", async () => {
		vi.mocked(mcp.taskGet).mockResolvedValue(null);
		await open("#projects?slug=tk-gone");

		await vi.waitFor(() =>
			expect(root.querySelector(".detail-panel")?.textContent).toContain(
				"No task",
			),
		);
		expect(root.querySelector('[role="alert"]')).toBeNull();
	});

	it("closes the panel on Escape, through the route", async () => {
		await open("#projects?slug=tk-fixture-000");
		await vi.waitFor(() =>
			expect(root.querySelector(".detail-panel")).not.toBeNull(),
		);

		document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));

		// Through the URL, so the back button and the close link agree with it.
		expect(window.location.hash).toBe("#projects");
		await vi.waitFor(() =>
			expect(root.querySelector(".detail-panel")).toBeNull(),
		);
	});

	it("ignores Escape when no panel is open", async () => {
		await open("#board");
		document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
		expect(window.location.hash).toBe("#board");
	});

	it("does not re-read the project when a task is opened beside it", async () => {
		// ★ Every navigation arrives as one `hashchange`, and a retarget blanks
		// the snapshot, so reacting to all of them flashes the rollup back to
		// "Reading this project…" on every open and every close.
		await open(`#projects?project=${projectDetail.slug}`);
		await vi.waitFor(() => expect(text()).toContain("Sessions"));
		const reads = vi.mocked(mcp.workflowProjectGet).mock.calls.length;

		window.location.hash = `#projects?project=${projectDetail.slug}&slug=tk-fixture-000`;
		await vi.waitFor(() =>
			expect(root.querySelector(".task-detail")).not.toBeNull(),
		);

		expect(vi.mocked(mcp.workflowProjectGet).mock.calls.length).toBe(reads);
		expect(text()).toContain("Sessions");
	});

	it("re-reads the project when the repo filter changes", async () => {
		// The repo IS an argument to the rollup's `task_list`, so this one has to.
		await open(`#projects?project=${projectDetail.slug}`);
		await vi.waitFor(() => expect(mcp.taskList).toHaveBeenCalledTimes(1));

		window.location.hash = `#projects?repo=https%3A%2F%2Fgithub.com%2Fmitodl%2Fhq&project=${projectDetail.slug}`;
		await vi.waitFor(() =>
			expect(mcp.taskList).toHaveBeenCalledWith(
				expect.objectContaining({ repo: "https://github.com/mitodl/hq" }),
			),
		);
	});

	it("does not re-read the open task when the project filter changes", async () => {
		await open(`#projects?slug=tk-fixture-000`);
		await vi.waitFor(() => expect(mcp.taskGet).toHaveBeenCalledTimes(1));

		window.location.hash = `#projects?project=${projectDetail.slug}&slug=tk-fixture-000`;
		await vi.waitFor(() => expect(mcp.workflowProjectGet).toHaveBeenCalled());

		expect(vi.mocked(mcp.taskGet).mock.calls.length).toBe(1);
	});

	it("re-reads when the open slug changes", async () => {
		await open("#projects?slug=tk-fixture-000");
		await vi.waitFor(() => expect(mcp.taskGet).toHaveBeenCalledTimes(1));

		window.location.hash = "#projects?slug=tk-fixture-004";
		await vi.waitFor(() =>
			expect(mcp.taskGet).toHaveBeenCalledWith("tk-fixture-004"),
		);
	});

	it("names an unbuilt view rather than rendering it empty", async () => {
		await open("#waves");
		expect(text()).toContain("not built yet");
	});

	it("does not read a rollup behind an unbuilt tab", async () => {
		// A project filter carried onto another tab kept four tool calls going
		// every 30s behind a body that says "not built yet", and showed a read
		// time for data nothing was rendering.
		await open(`#waves?project=${projectDetail.slug}`);
		await vi.waitFor(() => expect(text()).toContain("not built yet"));

		expect(mcp.workflowProjectGet).not.toHaveBeenCalled();
		expect(mcp.taskList).not.toHaveBeenCalled();
	});

	it("hands focus back to the link that opened the panel", async () => {
		await open("");
		await vi.waitFor(() =>
			expect(root.querySelector("tbody a")).not.toBeNull(),
		);

		const link = root.querySelector<HTMLAnchorElement>("tbody a");
		if (!link) {
			throw new Error("the project list rendered no links");
		}
		link.focus();
		window.location.hash = "#projects?slug=tk-fixture-000";
		await vi.waitFor(() =>
			expect(document.activeElement).toBe(root.querySelector(".detail-panel")),
		);

		document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));

		// Without this the panel leaves the DOM and focus resets to `<body>`, so
		// tabbing restarts at the top of the page rather than at the list.
		await vi.waitFor(() =>
			expect(document.activeElement).toBe(root.querySelector("tbody a")),
		);
	});

	it("stops polling when it stops", async () => {
		await open("");
		const before = vi.mocked(mcp.workflowProjectList).mock.calls.length;
		app.stop();

		window.dispatchEvent(new Event("focus"));
		expect(vi.mocked(mcp.workflowProjectList).mock.calls.length).toBe(before);
	});

	it("keeps the last good project list when a refresh fails", async () => {
		await open("");
		await vi.waitFor(() => expect(text()).toContain(projectDetail.title));

		vi.mocked(mcp.workflowProjectList).mockRejectedValue(new Error("gone"));
		window.dispatchEvent(new Event("focus"));

		await vi.waitFor(() => expect(text()).toContain("stale"));
		// The list is still on screen: a failed poll must not read as an empty graph.
		expect(text()).toContain(projectDetail.title);
	});
});

describe("reposIn", () => {
	it("unions and sorts the repos every project names", () => {
		expect(
			reposIn([
				{ ...(projects[0] as WorkflowProjectSummary), repos: ["b", "a"] },
				{ ...(projects[0] as WorkflowProjectSummary), repos: ["a", "c"] },
			]),
		).toEqual(["a", "b", "c"]);
	});

	it("tolerates a project with no repos", () => {
		expect(
			reposIn([{ ...(projects[0] as WorkflowProjectSummary), repos: null }]),
		).toEqual([]);
	});
});
