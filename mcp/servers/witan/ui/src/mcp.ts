import {
	Client,
	StreamableHTTPClientTransport,
} from "@modelcontextprotocol/client";
import type {
	Memory,
	MemoryKind,
	MemoryNeighbors,
	ProjectStatus,
	RecallResult,
	TaskDetail,
	TaskRow,
	TaskSearchRow,
	TopicResult,
	WorkflowProject,
	WorkflowProjectSummary,
	WorkflowSession,
} from "./types.js";
import { assertFlagsMatchServer, unwrap } from "./unwrap.js";

/**
 * The only file in the frontend that knows it is talking MCP.
 *
 * Every view calls the named functions at the bottom; none of them sees a
 * transport, an envelope or a tool name. That is what lets the widgets
 * (spec §7) and a later Tauri shell be presentations of one read layer rather
 * than three clients that drift.
 *
 * ★ THERE IS NO GENERIC `call(name, args)`, deliberately. ADR 0011 binds the
 * UI to an enumerated set of read tools, and a generic entry point would make
 * a fifteenth read a string somewhere rather than a visible diff to this file.
 * Writes are not here at all: doing them through this layer would duplicate
 * the tools' validation, and through the tools would widen the bound set to
 * mutations.
 */

/**
 * Same origin as the page: the server serving this bundle also serves /mcp.
 *
 * Resolved per connect rather than at module scope, and against `baseURI`
 * rather than `origin`. Both matter for the MCP Apps widgets this layer is
 * meant to serve: a widget runs in a sandboxed iframe, where
 * `window.location.origin` is the STRING "null" and the URL constructor
 * throws, at import time, taking the whole module graph down rather than one
 * call. `baseURI` also means a bundle mounted under a path prefix resolves
 * relative to it instead of assuming the root.
 */
function endpoint(): URL {
	return new URL("mcp", document.baseURI);
}

const PROTOCOL_ERA = "2026-07-28";

let connected: Promise<Client> | null = null;

/**
 * The shared client, connected on first use.
 *
 * Memoized rather than per-call: the connect is a round trip and a version
 * negotiation, and every view polls. A failed connect clears the memo so the
 * next read retries instead of resolving the same rejection forever.
 */
export function client(): Promise<Client> {
	if (!connected) {
		connected = connect().catch((error) => {
			connected = null;
			throw error;
		});
	}
	return connected;
}

async function connect(): Promise<Client> {
	const mcp = new Client(
		// Sent even though it is optional: fastmcp's `client_supports_extension`
		// reads `client_params`, which the server only builds when `clientInfo`
		// is present.
		{ name: "witan-ui", version: "0.0.0" },
		{
			// PINNED, not "auto". `auto` probes `server/discover` and falls back to
			// the 2025 handshake on any probe failure, so a misconfiguration would
			// quietly downgrade the protocol era instead of failing. Pinned, an era
			// mismatch is an error this page can show.
			versionNegotiation: { mode: { pin: PROTOCOL_ERA } },
		},
	);

	await mcp.connect(new StreamableHTTPClientTransport(endpoint()));

	// Once per page load. The wrap flags the unwrapper reads are recorded at
	// build time (a widget has no `tools/list` to consult), so this is what
	// stops that recording from silently rotting against a server whose return
	// annotations have moved.
	const { tools } = await mcp.listTools();
	assertFlagsMatchServer(
		tools.map((tool) => ({
			name: tool.name,
			outputSchema: tool.outputSchema as Record<string, unknown> | null,
		})),
	);

	return mcp;
}

/** Reset the memoized client. For tests, and for a login that replaces creds. */
export function resetClient(): void {
	connected = null;
}

/** A tool that ran and failed, as opposed to a transport that never reached it. */
export class ToolCallError extends Error {
	constructor(
		readonly tool: string,
		detail: string,
	) {
		super(`${tool} failed: ${detail}`);
		this.name = "ToolCallError";
	}
}

async function read<T>(
	tool: string,
	args: Record<string, unknown>,
): Promise<T> {
	const mcp = await client();
	let result: Awaited<ReturnType<typeof mcp.callTool>>;
	try {
		result = await mcp.callTool({ name: tool, arguments: args });
	} catch (error) {
		// A transport-level failure: the session is gone, the server restarted,
		// the page went offline. Drop the memoized client so the next read
		// reconnects instead of calling into a dead session for the life of the
		// page. A polling UI is exactly the shape that needs this.
		resetClient();
		throw error;
	}

	// ★ TOOL-LEVEL FAILURES ARE RETURNED, NOT THROWN. On `isError` the SDK
	// leaves `structuredContent` undefined, so unwrapping it would hand an
	// unwrapped tool `undefined` cast to its result type (the view then dies on
	// a property access, with the server's actual message thrown away), and a
	// wrapped one an error about the wrap machinery when the real cause is a
	// server-side exception.
	if (result.isError) {
		throw new ToolCallError(tool, describe(result.content));
	}

	return unwrap<T>(tool, result.structuredContent);
}

/** The text blocks of a failed call, which carry the server's message. */
function describe(content: unknown): string {
	if (!Array.isArray(content)) {
		return "no detail";
	}
	const text = content
		.filter(
			(block): block is { type: "text"; text: string } =>
				typeof block === "object" &&
				block !== null &&
				(block as { type?: unknown }).type === "text",
		)
		.map((block) => block.text)
		.join("\n");
	return text || "no detail";
}

// ── The bound set (ADR 0011 §3) ────────────────────────────────────
//
// `repo` is explicit on every call that takes it, including `""` for all
// repos, because the tools fall back to detecting a repo from the server's
// working directory. A deployed witan has no checkout, so an omitted `repo`
// means one thing locally and another on the deployment.

export function taskReady(args: {
	repo: string;
	project_slug?: string;
	assignee?: string;
	limit?: number;
}): Promise<TaskRow[]> {
	return read("task_ready", args);
}

/** `null` for a slug that does not exist. Not an error: a stale link is normal. */
export function taskGet(slug: string): Promise<TaskDetail | null> {
	return read("task_get", { slug });
}

export function taskList(args: {
	repo: string;
	status?: string;
	project_slug?: string;
	parent?: string;
	assignee?: string;
	limit?: number;
}): Promise<TaskRow[]> {
	return read("task_list", args);
}

/** Narrower rows than the list tools: no timestamps, plus `description`. */
export function taskSearch(args: {
	query: string;
	repo: string;
	status?: string;
}): Promise<TaskSearchRow[]> {
	return read("task_search", args);
}

export function workflowProjectGet(
	slug: string,
): Promise<WorkflowProject | null> {
	return read("workflow_project_get", { slug });
}

export function workflowProjectStatus(
	slug: string,
): Promise<ProjectStatus | null> {
	return read("workflow_project_status", { slug });
}

/** Summary rows: `github_issue` and no `github_pr`, unlike the single reads. */
export function workflowProjectList(args: {
	repo: string;
	status?: string;
	phase?: string;
	ready?: boolean;
}): Promise<WorkflowProjectSummary[]> {
	return read("workflow_project_list", args);
}

export function workflowSessionList(args: {
	project_slug?: string;
	open_only?: boolean;
	since?: string;
}): Promise<WorkflowSession[]> {
	return read("workflow_session_list", args);
}

/** A bare dict, not wrapped. The unwrapper knows; callers do not have to. */
export function recall(args: {
	query?: string;
	repo: string;
	task?: string;
	topic?: string;
}): Promise<RecallResult> {
	return read("recall", args);
}

export function memoryGet(slug: string): Promise<Memory | null> {
	return read("memory_get", { slug });
}

export function memoryList(args: {
	kind?: MemoryKind;
	repo: string;
	language?: string;
}): Promise<Memory[]> {
	return read("memory_list", args);
}

export function memorySearch(args: {
	query: string;
	repo: string;
	kind?: MemoryKind;
}): Promise<Memory[]> {
	return read("memory_search", args);
}

export function memoryNeighbors(args: {
	slug: string;
	kinds?: string[];
}): Promise<MemoryNeighbors> {
	return read("memory_neighbors", args);
}

/** `topic` is a `tp-` slug or a `name:kind` spec, e.g. `witan-ui:topic`. */
export function topicGet(topic: string): Promise<TopicResult | null> {
	return read("topic_get", { topic });
}
