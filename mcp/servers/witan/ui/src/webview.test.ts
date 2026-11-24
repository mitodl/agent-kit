import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import wrapFlags from "../fixtures/tools-list.json" with { type: "json" };

/**
 * The page inside an editor webview: reads go to the editor as messages, and
 * come back as the tool's value. The extension stands in here as a
 * `postMessage` that answers each request with a `message` event, as VS Code
 * delivers `webview.postMessage`.
 */

const ALL_TOOLS = Object.fromEntries(
	Object.keys(wrapFlags).map((name) => [name, ["repo", "all_repos"]]),
);

interface Request {
	witan: string;
	id: string;
	kind: "tools" | "read";
	tool?: string;
	args?: Record<string, unknown>;
}

function extension(
	answer: (request: Request) => { result: unknown } | { error: string },
) {
	const posted: Request[] = [];
	const state: { value: unknown } = { value: undefined };
	vi.stubGlobal("acquireVsCodeApi", () => ({
		postMessage(message: Request) {
			posted.push(message);
			queueMicrotask(() =>
				window.dispatchEvent(
					new MessageEvent("message", {
						data: { witan: "response", id: message.id, ...answer(message) },
					}),
				),
			);
		},
		getState: () => state.value,
		setState: (value: unknown) => {
			state.value = value;
		},
	}));
	return { posted, state };
}

beforeEach(() => {
	vi.resetModules();
});

afterEach(() => {
	vi.unstubAllGlobals();
	window.location.hash = "";
});

describe("in a webview", () => {
	it("reads through the editor, never over HTTP", async () => {
		const fetch = vi.fn();
		vi.stubGlobal("fetch", fetch);
		const { posted } = extension((request) =>
			request.kind === "tools"
				? { result: { repo: null, tools: ALL_TOOLS } }
				: { result: [{ slug: "tk-a" }] },
		);
		const { taskReady } = await import("./mcp.js");

		const rows = await taskReady({ repo: "" });

		expect(rows).toEqual([{ slug: "tk-a" }]);
		expect(fetch).not.toHaveBeenCalled();
		expect(
			posted.map(({ kind, tool, args }) => ({ kind, tool, args })),
		).toEqual([
			{ kind: "tools", tool: undefined, args: undefined },
			{ kind: "read", tool: "task_ready", args: { all_repos: true } },
		]);
	});

	it("answers the code graph and in_repo from the relayed tool list", async () => {
		const tools = { ...ALL_TOOLS, code_interface_providers: ["kind", "key"] };
		const { posted } = extension((request) =>
			request.kind === "tools"
				? { result: { repo: null, tools } }
				: { result: [] },
		);
		const { codeGraphAvailable, codeInterfaceProviders } = await import(
			"./mcp.js"
		);

		expect(await codeGraphAvailable()).toBe(true);
		await codeInterfaceProviders({
			kind: "env_var",
			key: "DATABASE_URL",
			in_repo: "https://github.com/example/web",
		});
		expect(posted.at(-1)?.args).toEqual({
			kind: "env_var",
			key: "DATABASE_URL",
		});
	});

	it("hides the bridge when the folder's routes lack witan-code", async () => {
		const { code_repo_dependencies: _, ...tools } = ALL_TOOLS;
		extension(() => ({ result: { repo: null, tools } }));
		const { codeGraphAvailable } = await import("./mcp.js");

		expect(await codeGraphAvailable()).toBe(false);
	});

	it("refuses to start without every required read", async () => {
		const { task_get: _, ...tools } = ALL_TOOLS;
		extension(() => ({ result: { repo: null, tools } }));
		const { taskReady } = await import("./mcp.js");

		await expect(taskReady({ repo: "" })).rejects.toThrow(/task_get/);
	});

	it("reports a failed read as the tool's failure, with the server's message", async () => {
		extension((request) =>
			request.kind === "tools"
				? { result: { repo: null, tools: ALL_TOOLS } }
				: { error: "Not logged in, run `witan login` first." },
		);
		const { taskGet, ToolCallError } = await import("./mcp.js");

		const failure = taskGet("tk-a");

		await expect(failure).rejects.toBeInstanceOf(ToolCallError);
		await expect(failure).rejects.toThrow(/task_get failed: Not logged in/);
	});

	it("keeps the route across the panel reloading", async () => {
		const { state } = extension(() => ({ result: null }));
		state.value = { hash: "#board?repo=x" };
		const { routeInWebview } = await import("./webview.js");

		routeInWebview();
		expect(window.location.hash).toBe("#board?repo=x");

		window.location.hash = "#memory";
		window.dispatchEvent(new HashChangeEvent("hashchange"));
		expect(state.value).toEqual({ hash: "#memory" });
	});

	it("goes where the editor navigates it", async () => {
		extension(() => ({ result: null }));
		const { routeInWebview } = await import("./webview.js");

		routeInWebview();
		window.dispatchEvent(
			new MessageEvent("message", {
				data: { witan: "navigate", hash: "#bridge" },
			}),
		);
		expect(window.location.hash).toBe("#bridge");
	});
});

describe("fragment links in a webview", () => {
	afterEach(() => {
		document.body.innerHTML = "";
	});

	it("are followed by the page, which VS Code's host would only scroll", async () => {
		extension(() => ({ result: null }));
		const { routeInWebview } = await import("./webview.js");
		routeInWebview();
		document.body.innerHTML = `<a href="#board?repo=x"><span>Board</span></a>`;

		const click = new MouseEvent("click", { bubbles: true, cancelable: true });
		document.querySelector("span")?.dispatchEvent(click);

		expect(window.location.hash).toBe("#board?repo=x");
		expect(click.defaultPrevented).toBe(true);
	});

	it("are left alone when a handler already took the click", async () => {
		extension(() => ({ result: null }));
		const { routeInWebview } = await import("./webview.js");
		routeInWebview();
		document.body.innerHTML = `<a href="#memory"><button>copy</button></a>`;
		document
			.querySelector("button")
			?.addEventListener("click", (event) => event.preventDefault());

		document
			.querySelector("button")
			?.dispatchEvent(
				new MouseEvent("click", { bubbles: true, cancelable: true }),
			);

		expect(window.location.hash).toBe("");
	});
});

describe("in a browser", () => {
	it("has no relay and leaves the route alone", async () => {
		const { hostRelay, inWebview, routeInWebview } = await import(
			"./webview.js"
		);

		routeInWebview();
		expect(inWebview()).toBe(false);
		expect(hostRelay()).toBeNull();
		expect(window.location.hash).toBe("");
	});
});

describe("createRelay", () => {
	it("matches each answer to its request and ignores other messages", async () => {
		const { createRelay } = await import("./webview.js");
		const target = new EventTarget();
		const posted: Request[] = [];
		const relay = createRelay(
			{ postMessage: (message) => posted.push(message as Request) },
			target as unknown as Window,
		);

		const first = relay.read("task_get", { slug: "a" });
		const second = relay.read("task_get", { slug: "b" });
		const send = (data: unknown) =>
			target.dispatchEvent(new MessageEvent("message", { data }));
		send("unrelated");
		send({
			witan: "response",
			id: "from-a-previous-load",
			result: "nobody asked",
		});
		send({ witan: "response", id: posted[1]?.id, result: "b" });
		send({ witan: "response", id: posted[0]?.id, result: "a" });

		expect(await first).toBe("a");
		expect(await second).toBe("b");
	});
});
