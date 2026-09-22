import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import contradictionsFixture from "../fixtures/memory_contradictions.json" with {
	type: "json",
};
import memoryGetFixture from "../fixtures/memory_get.json" with {
	type: "json",
};
import memoryListFixture from "../fixtures/memory_list.json" with {
	type: "json",
};
import neighborsFixture from "../fixtures/memory_neighbors.json" with {
	type: "json",
};
import recallFixture from "../fixtures/recall.json" with { type: "json" };
import taskGetFixture from "../fixtures/task_get.json" with { type: "json" };
import taskListFixture from "../fixtures/task_list.json" with { type: "json" };
import taskReadyFixture from "../fixtures/task_ready.json" with {
	type: "json",
};
import topicGetFixture from "../fixtures/topic_get.json" with { type: "json" };
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
	Memory,
	MemoryContradiction,
	MemoryNeighbors,
	ProjectStatus,
	RecallResult,
	TaskDetail,
	TaskRow,
	TopicResult,
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
	taskReady: vi.fn(),
	taskGet: vi.fn(),
	memoryContradictions: vi.fn(),
	memoryGet: vi.fn(),
	memoryList: vi.fn(),
	memoryNeighbors: vi.fn(),
	memorySearch: vi.fn(),
	recall: vi.fn(),
	topicGet: vi.fn(),
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
const ready = unwrap<TaskRow[]>("task_ready", taskReadyFixture);
const { TASK_LIMIT } = await import("./views/board.js");
const memory = unwrap<Memory>("memory_get", memoryGetFixture);

let root: HTMLElement;
let app: InstanceType<typeof App>;

beforeEach(() => {
	vi.mocked(mcp.workflowProjectList).mockResolvedValue(projects);
	vi.mocked(mcp.workflowProjectGet).mockResolvedValue(projectDetail);
	vi.mocked(mcp.workflowProjectStatus).mockResolvedValue(projectStatus);
	vi.mocked(mcp.workflowSessionList).mockResolvedValue(sessions);
	vi.mocked(mcp.taskList).mockImplementation(async (args) =>
		args.status ? tasks.filter((row) => row.status === args.status) : tasks,
	);
	vi.mocked(mcp.taskReady).mockResolvedValue(ready);
	vi.mocked(mcp.taskGet).mockResolvedValue(task);
	vi.mocked(mcp.memoryContradictions).mockResolvedValue(
		unwrap<MemoryContradiction[]>(
			"memory_contradictions",
			contradictionsFixture,
		),
	);
	vi.mocked(mcp.memoryGet).mockResolvedValue(memory);
	vi.mocked(mcp.memoryList).mockResolvedValue(
		unwrap<Memory[]>("memory_list", memoryListFixture),
	);
	vi.mocked(mcp.memoryNeighbors).mockResolvedValue(
		unwrap<MemoryNeighbors>("memory_neighbors", neighborsFixture),
	);
	vi.mocked(mcp.memorySearch).mockResolvedValue(
		unwrap<Memory[]>("memory_list", memoryListFixture),
	);
	vi.mocked(mcp.recall).mockResolvedValue(
		unwrap<RecallResult>("recall", recallFixture),
	);
	vi.mocked(mcp.topicGet).mockResolvedValue(
		unwrap<TopicResult>("topic_get", topicGetFixture),
	);

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
		// ★ NO `limit`, and `repo: ""`. The by-project query is uncapped, so a
		// limit can only drop tasks past it; and `task_list` returns early on
		// `project_slug`, before `repo` is read, so a repo here is a no-op that
		// would only look like a filter.
		expect(mcp.taskList).toHaveBeenCalledWith({
			repo: "",
			project_slug: projectDetail.slug,
		});
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

	it("does not re-read the project when the repo filter changes", async () => {
		// None of the four reads takes a repo that does anything, so a repo
		// change cannot alter this rollup. Re-reading on it bought four tool
		// calls and a blanked body for an identical result.
		await open(`#projects?project=${projectDetail.slug}`);
		await vi.waitFor(() => expect(mcp.taskList).toHaveBeenCalledTimes(1));

		window.location.hash = `#projects?repo=https%3A%2F%2Fgithub.com%2Fmitodl%2Fhq&project=${projectDetail.slug}`;
		await vi.waitFor(() => expect(window.location.hash).toContain("mitodl"));
		await Promise.resolve();

		expect(mcp.taskList).toHaveBeenCalledTimes(1);
		expect(mcp.workflowProjectGet).toHaveBeenCalledTimes(1);
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

	it("reads Ready with the route's scope and a limit that cannot truncate it", async () => {
		await open("#board?repo=https%3A%2F%2Fgithub.com%2Fmitodl%2Fagent-kit");
		await vi.waitFor(() => expect(text()).toContain("In progress"));

		// ★ `task_ready` sorts THEN truncates, so its default 20 would push
		// ready tasks into Blocked on any board with more than 20 of them.
		expect(mcp.taskReady).toHaveBeenCalledWith({
			repo: "https://github.com/mitodl/agent-kit",
			limit: TASK_LIMIT,
		});
	});

	it("reads the live statuses across every repo, and no closed tasks", async () => {
		await open("#board?repo=https%3A%2F%2Fgithub.com%2Fmitodl%2Fagent-kit");
		await vi.waitFor(() => expect(text()).toContain("In progress"));

		// ★ All repos regardless of the filter: a blocked card has to know
		// whether a blocker in another repo is still open.
		for (const status of ["open", "blocked", "in_progress"]) {
			expect(mcp.taskList).toHaveBeenCalledWith({
				repo: "",
				status,
				limit: TASK_LIMIT,
			});
		}
		expect(mcp.taskList).not.toHaveBeenCalledWith(
			expect.objectContaining({ status: "closed" }),
		);
	});

	it("scopes a project board by project, as the tools do", async () => {
		await open(`#board?project=${projectDetail.slug}&closed=1`);
		await vi.waitFor(() => expect(text()).toContain("Closed"));

		expect(mcp.taskReady).toHaveBeenCalledWith({
			repo: "",
			project_slug: projectDetail.slug,
			limit: TASK_LIMIT,
		});
		expect(mcp.taskList).toHaveBeenCalledWith({
			repo: "",
			project_slug: projectDetail.slug,
			status: "closed",
			limit: TASK_LIMIT,
		});
		// Not the rollup: that is the Projects tab's read.
		expect(mcp.workflowProjectGet).not.toHaveBeenCalled();
	});

	it("does not re-read the board when a card is opened beside it", async () => {
		await open("#board");
		await vi.waitFor(() => expect(text()).toContain("In progress"));
		const reads = vi.mocked(mcp.taskReady).mock.calls.length;

		window.location.hash = "#board?slug=tk-fixture-000";
		await vi.waitFor(() =>
			expect(root.querySelector(".detail-panel")).not.toBeNull(),
		);

		expect(vi.mocked(mcp.taskReady).mock.calls.length).toBe(reads);
	});

	it("re-reads the board when its scope changes", async () => {
		await open("#board");
		await vi.waitFor(() => expect(text()).toContain("In progress"));

		window.location.hash = "#board?closed=1";
		await vi.waitFor(() =>
			expect(mcp.taskList).toHaveBeenCalledWith({
				repo: "",
				status: "closed",
				limit: TASK_LIMIT,
			}),
		);
	});

	it("stops reading the board when another tab is chosen", async () => {
		await open("#board");
		await vi.waitFor(() => expect(text()).toContain("In progress"));
		vi.mocked(mcp.taskReady).mockClear();

		window.location.hash = "#waves";
		await vi.waitFor(() => expect(text()).toContain("not built yet"));
		window.dispatchEvent(new Event("focus"));

		expect(mcp.taskReady).not.toHaveBeenCalled();
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

	it("reads the timeline's sessions from the window's start", async () => {
		const before = Date.now();
		await open("#timeline?days=7");
		await vi.waitFor(() =>
			expect(root.querySelector(".timeline")).not.toBeNull(),
		);

		expect(mcp.taskList).toHaveBeenCalledWith({ repo: "", limit: TASK_LIMIT });
		// Every status: a completed project still needs its title and sessions.
		expect(mcp.workflowProjectList).toHaveBeenCalledWith({
			repo: "",
			status: null,
		});
		const [args] = vi.mocked(mcp.workflowSessionList).mock.calls[0] ?? [];
		const since = Date.parse(args?.since ?? "");
		// Seven days back from the moment of the read, not from some fixed day.
		expect(since).toBeGreaterThanOrEqual(before - 7 * 86_400_000);
		expect(since).toBeLessThanOrEqual(Date.now() - 7 * 86_400_000);
		expect(args?.project_slug).toBeUndefined();
	});

	it("narrows both timeline reads to a filtered project", async () => {
		await open(`#timeline?project=${projectDetail.slug}`);
		await vi.waitFor(() =>
			expect(root.querySelector(".timeline")).not.toBeNull(),
		);

		expect(mcp.taskList).toHaveBeenCalledWith({
			repo: "",
			project_slug: projectDetail.slug,
		});
		expect(mcp.workflowSessionList).toHaveBeenCalledWith(
			expect.objectContaining({ project_slug: projectDetail.slug }),
		);
	});

	it("does not re-read the timeline for a repo change", async () => {
		// Both reads are repo-wide and the repo narrows them in the browser.
		await open("#timeline");
		await vi.waitFor(() =>
			expect(root.querySelector(".timeline")).not.toBeNull(),
		);
		const reads = vi.mocked(mcp.taskList).mock.calls.length;

		window.location.hash =
			"#timeline?repo=https%3A%2F%2Fgithub.com%2Fmitodl%2Fhq";
		// The fixture's in-progress task is in agent-kit, so hq draws nothing.
		await vi.waitFor(() => expect(text()).toContain("Nothing was worked"));

		expect(vi.mocked(mcp.taskList).mock.calls.length).toBe(reads);
	});

	it("re-reads the timeline when the window changes", async () => {
		await open("#timeline");
		await vi.waitFor(() =>
			expect(root.querySelector(".timeline")).not.toBeNull(),
		);

		window.location.hash = "#timeline?days=30";
		await vi.waitFor(() =>
			expect(vi.mocked(mcp.workflowSessionList).mock.calls.length).toBe(2),
		);
	});

	it("does not poll the timeline on an interval", async () => {
		// Spec §6.6: it plots elapsed time, so it re-reads on focus and Refresh.
		vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
		try {
			await open("#timeline");
			await vi.waitFor(() =>
				expect(root.querySelector(".timeline")).not.toBeNull(),
			);
			const reads = vi.mocked(mcp.taskList).mock.calls.length;

			vi.advanceTimersByTime(5 * 60_000);
			expect(vi.mocked(mcp.taskList).mock.calls.length).toBe(reads);

			window.dispatchEvent(new Event("focus"));
			expect(vi.mocked(mcp.taskList).mock.calls.length).toBe(reads + 1);
		} finally {
			vi.useRealTimers();
		}
	});

	it("lands the memory view on the inbox over a browse list", async () => {
		await open("#memory?repo=https%3A%2F%2Fgithub.com%2Fmitodl%2Fagent-kit");
		await vi.waitFor(() => expect(root.querySelector(".inbox")).not.toBeNull());

		const repo = "https://github.com/mitodl/agent-kit";
		expect(mcp.memoryList).toHaveBeenCalledWith({
			repo,
			kind: undefined,
			include_superseded: false,
		});
		expect(mcp.memoryContradictions).toHaveBeenCalledWith({ repo });
		expect(mcp.recall).not.toHaveBeenCalled();
	});

	it("reads each side of a pair once, however many pairs it is in", async () => {
		const [pair] = unwrap<MemoryContradiction[]>(
			"memory_contradictions",
			contradictionsFixture,
		);
		if (!pair) {
			throw new Error("the fixture has no pair");
		}
		vi.mocked(mcp.memoryContradictions).mockResolvedValue([pair, pair]);
		await open("#memory");
		await vi.waitFor(() => expect(root.querySelector(".inbox")).not.toBeNull());

		expect(
			vi
				.mocked(mcp.memoryGet)
				.mock.calls.map(([slug]) => slug)
				.sort(),
		).toEqual([pair.a.slug, pair.b.slug].sort());
	});

	it("keeps resolved pairs out of the inbox when superseded memories are shown", async () => {
		// The toggle widens the list; a resolved pair offering "Resolve" again
		// would read as open.
		await open("#memory?superseded=1");
		await vi.waitFor(() => expect(root.querySelector(".inbox")).not.toBeNull());

		expect(mcp.memoryContradictions).toHaveBeenCalledWith({ repo: "" });
		expect(mcp.memoryList).toHaveBeenCalledWith({
			repo: "",
			kind: undefined,
			include_superseded: true,
		});
		expect(mcp.memoryGet).not.toHaveBeenCalledWith(expect.anything(), {
			topics: true,
		});
	});

	it("searches through recall, or memory_search when plain", async () => {
		await open("#memory?q=unwrap&kind=pattern&superseded=1");
		await vi.waitFor(() =>
			expect(root.querySelector(".recall-pairs")).not.toBeNull(),
		);
		expect(mcp.recall).toHaveBeenCalledWith({
			query: "unwrap",
			repo: "",
			kind: "pattern",
			include_superseded: true,
		});
		expect(mcp.memoryContradictions).not.toHaveBeenCalled();

		window.location.hash = "#memory?q=unwrap&plain=1";
		await vi.waitFor(() =>
			expect(mcp.memorySearch).toHaveBeenCalledWith({
				query: "unwrap",
				repo: "",
				kind: undefined,
				include_superseded: false,
			}),
		);
	});

	it("shows one topic's memories", async () => {
		await open("#memory?topic=tp-topic-witan-ui");
		await vi.waitFor(() =>
			expect(root.querySelector(".topic-heading")).not.toBeNull(),
		);

		expect(mcp.topicGet).toHaveBeenCalledWith("tp-topic-witan-ui");
		expect(mcp.memoryList).not.toHaveBeenCalled();
	});

	it("narrows a topic by kind in the browser, since topic_get takes none", async () => {
		await open("#memory?topic=tp-topic-witan-ui&kind=lesson");
		await vi.waitFor(() =>
			expect(root.querySelector(".topic-heading")).not.toBeNull(),
		);

		const kinds = [...root.querySelectorAll("tbody .badge")].map(
			(badge) => badge.textContent,
		);
		expect(kinds.length).toBeGreaterThan(0);
		expect(new Set(kinds)).toEqual(new Set(["lesson"]));
	});

	it("opens a memory slug as a memory, not a task", async () => {
		await open(`#memory?slug=${memory.slug}`);
		await vi.waitFor(() =>
			expect(root.querySelector(".memory-detail")).not.toBeNull(),
		);

		expect(mcp.memoryNeighbors).toHaveBeenCalledWith({ slug: memory.slug });
		expect(mcp.memoryGet).toHaveBeenCalledWith(memory.slug, { topics: true });
		expect(mcp.taskGet).not.toHaveBeenCalled();
		expect(root.querySelector(".detail-panel h2")?.textContent).toBe(
			memory.title,
		);
	});

	it("treats a null memory as a stale link, not a failure", async () => {
		vi.mocked(mcp.memoryGet).mockResolvedValue(null);
		await open("#memory?slug=pat-gone");

		await vi.waitFor(() =>
			expect(root.querySelector(".detail-panel")?.textContent).toContain(
				"No memory",
			),
		);
		expect(root.querySelector('[role="alert"]')).toBeNull();
	});

	it("does not re-read the memory list when a memory is opened beside it", async () => {
		await open("#memory");
		await vi.waitFor(() => expect(mcp.memoryList).toHaveBeenCalledTimes(1));

		window.location.hash = `#memory?slug=${memory.slug}`;
		await vi.waitFor(() =>
			expect(root.querySelector(".memory-detail")).not.toBeNull(),
		);

		expect(mcp.memoryList).toHaveBeenCalledTimes(1);
		expect(root.querySelector(".inbox")).not.toBeNull();
	});

	it("does not read memories behind another tab", async () => {
		await open("#projects?q=unwrap");
		await vi.waitFor(() => expect(mcp.workflowProjectList).toHaveBeenCalled());

		expect(mcp.recall).not.toHaveBeenCalled();
		expect(mcp.memoryList).not.toHaveBeenCalled();
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

	it("marks the panel stale when a detail poll fails over a shown task", async () => {
		// ★ The top bar reports the view UNDERNEATH, so without a status line of
		// its own the panel showed a retained task with no stale marker anywhere
		// and a Refresh that retried the wrong read — the one case the
		// retain-last-good rule exists for, silently unreported.
		await open("#projects?slug=tk-fixture-000");
		await vi.waitFor(() =>
			expect(root.querySelector(".task-detail")).not.toBeNull(),
		);

		vi.mocked(mcp.taskGet).mockRejectedValue(new Error("server went away"));
		window.dispatchEvent(new Event("focus"));

		const panel = () => root.querySelector(".detail-panel");
		await vi.waitFor(() =>
			expect(panel()?.querySelector(".stale")).not.toBeNull(),
		);
		// The task is still on screen; only the marker is new.
		expect(panel()?.textContent).toContain(task.title);
	});

	it("refreshes the panel's own read, not the view underneath", async () => {
		await open("#projects?slug=tk-fixture-000");
		await vi.waitFor(() =>
			expect(root.querySelector(".task-detail")).not.toBeNull(),
		);
		const listReads = vi.mocked(mcp.workflowProjectList).mock.calls.length;
		const detailReads = vi.mocked(mcp.taskGet).mock.calls.length;

		root.querySelector<HTMLButtonElement>(".detail-panel button")?.click();

		await vi.waitFor(() =>
			expect(vi.mocked(mcp.taskGet).mock.calls.length).toBe(detailReads + 1),
		);
		expect(vi.mocked(mcp.workflowProjectList).mock.calls.length).toBe(
			listReads,
		);
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
