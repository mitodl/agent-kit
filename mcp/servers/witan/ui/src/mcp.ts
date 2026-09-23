import {
	Client,
	StreamableHTTPClientTransport,
} from "@modelcontextprotocol/client";
import { type AuthConfig, bearerAuth } from "./auth.js";
import type {
	Memory,
	MemoryContradiction,
	MemoryKind,
	MemoryNeighbors,
	ProjectStatus,
	RecallResult,
	TaskDetail,
	TaskRow,
	TaskSearchRow,
	TopicResult,
	WorkflowProjectDetail,
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

/** The path segment the bundle is mounted under. */
const MOUNT = "/ui/";

/** What `/ui/config.json` carries. */
export interface UiConfig {
	auth: AuthConfig | null;
	/** Where the protocol is served, which `--path` can move. */
	mcp_path: string;
}

/**
 * Where the protocol endpoint is, given the document's URL and the server's
 * own configured path.
 *
 * ★ TWO THINGS ARE VARIABLE HERE, AND ASSUMING EITHER ONE HAS BROKEN THIS.
 *
 * The MOUNT PREFIX, because the bundle can sit behind a reverse-proxy path.
 * Anchoring on the mount segment rather than walking up a fixed number of
 * levels is what makes that independent of route depth:
 *
 *   "mcp"     -> /ui/mcp   wrong from any document
 *   "../mcp"  -> /mcp      right, but only from /ui/<one-segment>
 *
 * The second is what a hash-routed app happens to produce. Spec §6.1 wants
 * the URL to carry the view, the filters and the open slug, so the first view
 * using `history.pushState` puts the document at /ui/board/tk-x and "../mcp"
 * resolves to /ui/mcp again.
 *
 * The PROTOCOL PATH, because `witan serve --path` is a public option and the
 * `/ui/` routes do not move with it. It comes from `/ui/config.json` rather
 * than being assumed, so a server on /api/mcp serves a page that reads.
 *
 * Resolved per connect rather than at module scope, so a document whose URL
 * the constructor rejects (an `about:srcdoc` widget frame) throws on one call
 * instead of taking the module graph down at import.
 */
export function mcpEndpoint(documentUrl: string, mcpPath: string): URL {
	const here = new URL(documentUrl);
	const cut = here.pathname.lastIndexOf(MOUNT);
	const prefix = cut === -1 ? "/" : here.pathname.slice(0, cut + 1);
	// The configured path is absolute on the server ("/api/mcp"); it joins the
	// mount prefix rather than replacing it, so a proxy prefix survives.
	return new URL(`${prefix}${mcpPath.replace(/^\//, "")}`, here);
}

/**
 * The path the bundle is mounted at, through the mount segment: `/ui/`, or
 * `/witan/ui/` behind a proxy prefix. It is where `config.json` lives and the
 * login's redirect URI. A document outside any mount (a dev server) gets `/`.
 */
export function mountBase(documentUrl: string): string {
	const { pathname } = new URL(documentUrl);
	const cut = pathname.lastIndexOf(MOUNT);
	return cut === -1 ? "/" : pathname.slice(0, cut + MOUNT.length);
}

/** Fetch `/ui/config.json`, which the page reads before anything else. */
async function uiConfig(): Promise<UiConfig> {
	const here = new URL(document.baseURI);
	const response = await fetch(
		new URL(`${mountBase(here.href)}config.json`, here),
	);
	if (!response.ok) {
		throw new Error(
			`/ui/config.json answered ${response.status}; the page cannot find the ` +
				"protocol endpoint without it.",
		);
	}
	return (await response.json()) as UiConfig;
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

	const config = await uiConfig();
	// Undefined locally, where the page sends no credential. Deployed, this is
	// where the login happens, before the first request that would 401.
	const authProvider = await bearerAuth(
		config.auth,
		mountBase(document.baseURI),
	);
	await mcp.connect(
		new StreamableHTTPClientTransport(
			mcpEndpoint(document.baseURI, config.mcp_path),
			{ authProvider },
		),
	);

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

/** The DETAIL shape: nine fields more than the rollup's `project`. */
export function workflowProjectGet(
	slug: string,
): Promise<WorkflowProjectDetail | null> {
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
	/** Omitted, the tool lists `active` projects only; `null` lists every status. */
	status?: string | null;
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
	kind?: MemoryKind;
	limit?: number;
	hops?: number;
	include_superseded?: boolean;
}): Promise<RecallResult> {
	return read("recall", args);
}

/** `topics` attaches each Tagged edge, which costs a second graph read. */
export function memoryGet(
	slug: string,
	options: { topics?: boolean } = {},
): Promise<Memory | null> {
	return read("memory_get", { slug, include_topics: options.topics ?? false });
}

export function memoryList(args: {
	kind?: MemoryKind;
	repo: string;
	language?: string;
	include_superseded?: boolean;
}): Promise<Memory[]> {
	return read("memory_list", args);
}

export function memorySearch(args: {
	query: string;
	repo: string;
	kind?: MemoryKind;
	include_superseded?: boolean;
}): Promise<Memory[]> {
	return read("memory_search", args);
}

export function memoryNeighbors(args: {
	slug: string;
	kinds?: string[];
}): Promise<MemoryNeighbors> {
	return read("memory_neighbors", args);
}

/**
 * Every contradicting pair, newest link first. A pair is in scope when either
 * memory is in `repo`. A pair with a superseded side is resolved, and dropped
 * unless `include_superseded`.
 */
export function memoryContradictions(args: {
	repo: string;
	include_superseded?: boolean;
}): Promise<MemoryContradiction[]> {
	return read("memory_contradictions", args);
}

/** `topic` is a `tp-` slug or a `name:kind` spec, e.g. `witan-ui:topic`. */
export function topicGet(topic: string): Promise<TopicResult | null> {
	return read("topic_get", { topic });
}
