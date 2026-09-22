import type { JSONRPCMessage, Transport } from "@modelcontextprotocol/client";
import type { App } from "@modelcontextprotocol/ext-apps";
import { AppBridge } from "@modelcontextprotocol/ext-apps/app-bridge";
import type { TemplateResult } from "lit-html";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import recallFixture from "../../fixtures/recall.json" with { type: "json" };
import taskListFixture from "../../fixtures/task_list.json" with {
	type: "json",
};
import taskReadyFixture from "../../fixtures/task_ready.json" with {
	type: "json",
};
import statusFixture from "../../fixtures/workflow_project_status.json" with {
	type: "json",
};
import type { ProjectStatus, RecallResult, TaskRow } from "../types.js";
import { unwrap } from "../unwrap.js";
import { readyColumn } from "../views/board.js";
import { recallResults } from "../views/memory.js";
import { statusRollup, taskGroups } from "../views/projects.js";
import { mountWidget, WIDGET_ROUTE, type WidgetInput } from "./host.js";

/**
 * The widgets driven through the real ext-apps handshake: an `AppBridge`
 * playing the host on one end of an in-memory pair, the widget's `App` on the
 * other. The host sends the RECORDED tool result, exactly as a host relays a
 * `tools/call` response, so what is asserted is what a widget does with a
 * result the server actually produces.
 */

/** Two transports wired back to back. The SDK ships none for the browser. */
function transportPair(): [Transport, Transport] {
	const make = (): Transport & { peer?: Transport } => ({
		async start() {},
		async send(message: JSONRPCMessage) {
			queueMicrotask(() => this.peer?.onmessage?.(message));
		},
		async close() {
			this.onclose?.();
		},
	});
	const a = make();
	const b = make();
	a.peer = b;
	b.peer = a;
	return [a, b];
}

let root: HTMLElement;
let bridge: AppBridge;

let app: App | undefined;

beforeEach(() => {
	root = document.createElement("div");
	document.body.replaceChildren(root);
	// `App.connect` watches the document to report its size to the host, and
	// jsdom has no ResizeObserver. Without one the handshake rejects.
	vi.stubGlobal(
		"ResizeObserver",
		class {
			observe() {}
			disconnect() {}
		},
	);
});

afterEach(async () => {
	// The size report `App` sends on an animation frame would otherwise land
	// after the close below and reject as "Not connected".
	await new Promise((resolve) => requestAnimationFrame(resolve));
	await app?.close();
	await bridge?.close();
	vi.unstubAllGlobals();
	document.documentElement.removeAttribute("data-theme");
});

/** Mount a widget and complete the handshake. Resolves once the widget is live. */
async function mount<T>(
	tool: string,
	draw: (input: WidgetInput<T>) => TemplateResult,
	theme?: "light" | "dark",
): Promise<AppBridge> {
	const [host, view] = transportPair();
	bridge = new AppBridge(
		null,
		{ name: "test-host", version: "1.0.0" },
		{ openLinks: {} },
		{ hostContext: theme ? { theme } : {} },
	);
	const initialized = new Promise<void>((resolve) => {
		bridge.oninitialized = () => resolve();
	});
	await bridge.connect(host);
	app = mountWidget(tool, draw, { root, transport: view });
	await initialized;
	return bridge;
}

/** Send a result and wait for the widget to draw it. */
async function send(structuredContent: unknown, isError = false) {
	await bridge.sendToolResult({
		content: [{ type: "text", text: JSON.stringify(structuredContent) }],
		structuredContent: structuredContent as Record<string, unknown>,
		isError,
	});
	await vi.waitFor(() => expect(root.querySelector(".placeholder")).toBeNull());
}

const NOW = Date.parse("2026-01-01T06:00:00Z");

describe("every widget", () => {
	it("says it is waiting until the host sends a result", async () => {
		await mount<TaskRow[]>("task_ready", ({ value, route }) =>
			readyColumn(value, route, NOW),
		);

		expect(root.textContent).toContain("Waiting for task_ready");
	});

	it("shows the tool's own error text when the call failed", async () => {
		await mount<TaskRow[]>("task_ready", ({ value, route }) =>
			readyColumn(value, route, NOW),
		);

		await bridge.sendToolResult({
			content: [{ type: "text", text: "store is locked" }],
			isError: true,
		});

		await vi.waitFor(() =>
			expect(root.querySelector("[role=alert]")?.textContent).toContain(
				"store is locked",
			),
		);
	});

	it("says the call was cancelled rather than waiting forever", async () => {
		await mount<TaskRow[]>("task_ready", ({ value, route }) =>
			readyColumn(value, route, NOW),
		);

		await bridge.sendToolCancelled({ reason: "user stopped it" });

		await vi.waitFor(() =>
			expect(root.querySelector("[role=alert]")?.textContent).toContain(
				"cancelled: user stopped it",
			),
		);
	});

	it("reports a result that does not unwrap rather than drawing nothing", async () => {
		// task_ready is recorded as wrapped; a bare list is not what it sends.
		await mount<TaskRow[]>("task_ready", ({ value, route }) =>
			readyColumn(value, route, NOW),
		);

		await send([]);

		expect(root.querySelector("[role=alert]")?.textContent).toContain(
			'no "result" key',
		);
	});

	it("takes the host's theme over the OS preference", async () => {
		await mount<TaskRow[]>(
			"task_ready",
			({ value, route }) => readyColumn(value, route, NOW),
			"dark",
		);

		await vi.waitFor(() =>
			expect(document.documentElement.getAttribute("data-theme")).toBe("dark"),
		);
	});

	it("hands an external link to the host and keeps route links inert", async () => {
		const opened = vi.fn<(url: string) => void>();
		await mount<ProjectStatus | null>(
			"workflow_project_status",
			({ value, route }) => statusRollup(value, route, NOW),
		);
		bridge.onopenlink = async (params) => {
			opened(params.url);
			return {};
		};
		const status = unwrap<ProjectStatus>(
			"workflow_project_status",
			statusFixture,
		);
		await send({
			result: {
				...status,
				project: {
					...status.project,
					github_pr: "HTTPS://github.com/o/r/pull/1",
				},
			},
		});

		const external = root.querySelector<HTMLAnchorElement>(
			'a[href^="HTTPS://"]',
		);
		const click = new MouseEvent("click", { bubbles: true, cancelable: true });
		external?.dispatchEvent(click);

		expect(click.defaultPrevented).toBe(true);
		await vi.waitFor(() =>
			expect(opened).toHaveBeenCalledWith("https://github.com/o/r/pull/1"),
		);

		const internal = root.querySelector<HTMLAnchorElement>('a[href^="#"]');
		const inert = new MouseEvent("click", { bubbles: true, cancelable: true });
		internal?.dispatchEvent(inert);
		expect(inert.defaultPrevented).toBe(true);
		expect(opened).toHaveBeenCalledTimes(1);
	});
});

describe("the task_ready widget", () => {
	it("draws the Ready column from the result and nothing it was not handed", async () => {
		await mount<TaskRow[]>("task_ready", ({ value, route }) =>
			readyColumn(value, route, NOW),
		);
		const ready = unwrap<TaskRow[]>("task_ready", taskReadyFixture);

		await send(taskReadyFixture);

		const cards = root.querySelectorAll(".card");
		expect(cards).toHaveLength(ready.length);
		expect(root.querySelector("[aria-label=Ready] .count")?.textContent).toBe(
			String(ready.length),
		);
		expect(root.textContent).not.toContain("In progress");
		expect(root.textContent).not.toContain("Blocked");
		// Spec §7.1: an unscoped result spans projects, so each card names its own.
		expect(
			[...root.querySelectorAll(".card-meta code")].map((el) => el.textContent),
		).toEqual(ready.map((task) => task.project_slug));
	});
});

describe("the workflow_project_status widget", () => {
	it("draws the rollup's facts, ready list and last session", async () => {
		await mount<ProjectStatus | null>(
			"workflow_project_status",
			({ value, route }) => statusRollup(value, route, NOW),
		);
		const status = unwrap<ProjectStatus>(
			"workflow_project_status",
			statusFixture,
		);

		await send(statusFixture);

		expect(root.querySelector("h2")?.textContent).toBe(status.project.title);
		expect(root.querySelectorAll("ul.ready li")).toHaveLength(
			status.ready_tasks.length,
		);
		expect(root.textContent).toContain(status.last_session?.summary ?? "");
	});

	it("says the project does not exist when the tool returned null", async () => {
		await mount<ProjectStatus | null>(
			"workflow_project_status",
			({ value, route }) => statusRollup(value, route, NOW),
		);

		await send({ result: null });

		expect(root.textContent).toContain("No such project");
	});
});

describe("the recall widget", () => {
	it("calls out the contradiction pairs and marks both memories", async () => {
		await mount<RecallResult>("recall", ({ value, args, route }) =>
			recallResults(value, { ...route, q: String(args.query ?? "") }, NOW),
		);
		const recall = unwrap<RecallResult>("recall", recallFixture);

		await send(recallFixture);

		expect(root.querySelectorAll(".recall-pairs li")).toHaveLength(
			recall.contradictions.length,
		);
		expect(root.querySelectorAll(".badge.contradicted")).toHaveLength(2);
		expect(root.querySelectorAll("table.memories tbody tr")).toHaveLength(
			recall.memories.length,
		);
	});

	it("names the model's query when nothing was recalled", async () => {
		await mount<RecallResult>("recall", ({ value, args, route }) =>
			recallResults(value, { ...route, q: String(args.query ?? "") }, NOW),
		);

		await bridge.sendToolInput({ arguments: { query: "gantt" } });
		await send({ memories: [], seeds: [], contradictions: [] });

		expect(root.textContent).toContain("Nothing matches “gantt”");
	});
});

describe("the task_list widget", () => {
	it("groups the rows by their status", async () => {
		await mount<TaskRow[]>("task_list", ({ value, route }) =>
			taskGroups(value, route, NOW),
		);
		const tasks = unwrap<TaskRow[]>("task_list", taskListFixture);

		await send(taskListFixture);

		const headings = [...root.querySelectorAll("h3")].map((h) =>
			h.textContent?.replace(/\s+/g, " ").trim(),
		);
		expect(headings).toEqual(
			["In progress", "Open", "Blocked"].map(
				(label) =>
					`${label} ${
						tasks.filter(
							(task) =>
								task.status ===
								{
									"In progress": "in_progress",
									Open: "open",
									Blocked: "blocked",
								}[label],
						).length
					}`,
			),
		);
		expect(root.querySelectorAll("tbody tr")).toHaveLength(tasks.length);
	});

	it("shows closed rows the call returned", () => {
		expect(WIDGET_ROUTE.closed).toBe(true);
	});
});
