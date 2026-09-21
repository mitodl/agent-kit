import wrapFlags from "../fixtures/tools-list.json" with { type: "json" };

/**
 * Whether each bound tool's result is wrapped in `{ result: ... }`.
 *
 * ★ THIS IS A LOOKUP, NOT A GUESS, and the distinction is the whole point.
 * fastmcp wraps a tool whose return annotation is a list or a `dict | None`
 * and marks the schema `x-fastmcp-wrap-result`; a tool returning a bare `dict`
 * is not wrapped. Inferring that from the value at runtime gets it wrong in
 * both directions: a wrapped `list[dict]` and an unwrapped `dict` with a
 * `result` key are indistinguishable, and `{ "result": null }` looks like an
 * absent value rather than a present null.
 *
 * Recorded at build time rather than read from `tools/list` because an MCP
 * Apps widget (spec §7) is handed a result with no `tools/list` to consult.
 * `assertFlagsMatchServer` checks the recording against the live server once
 * per page load, so a server that changes a return annotation is a loud
 * failure rather than a view that silently renders nothing.
 */
const WRAPPED: Record<string, boolean> = Object.fromEntries(
	Object.entries(wrapFlags as Record<string, { wrapped: boolean }>).map(
		([name, info]) => [name, info.wrapped],
	),
);

export const BOUND_TOOLS = Object.keys(WRAPPED).sort();

export class UnknownToolError extends Error {
	constructor(name: string) {
		super(
			`${name} is not in the bound read set. Adding a tool means amending ADR 0011 and regenerating the fixtures with \`just ui-fixtures\`.`,
		);
		this.name = "UnknownToolError";
	}
}

export class WrapFlagMismatchError extends Error {
	constructor(readonly mismatches: string[]) {
		super(
			`The server's result wrapping no longer matches the recorded fixtures (${mismatches.join(", ")}). Run \`just ui-fixtures\` against this server.`,
		);
		this.name = "WrapFlagMismatchError";
	}
}

/**
 * Take the value a tool returned out of its MCP envelope.
 *
 * `null` is a value here, not an absence: `task_get` of a slug that does not
 * exist is `{ result: null }`, and a page that treats it as an error shows a
 * crash for a stale link. That case has its own fixture.
 */
export function unwrap<T>(tool: string, structuredContent: unknown): T {
	const wrapped = WRAPPED[tool];
	if (wrapped === undefined) {
		throw new UnknownToolError(tool);
	}
	if (!wrapped) {
		return structuredContent as T;
	}
	if (
		structuredContent === null ||
		typeof structuredContent !== "object" ||
		!("result" in structuredContent)
	) {
		throw new Error(
			`${tool} is recorded as wrapped but its result has no "result" key.`,
		);
	}
	return (structuredContent as { result: unknown }).result as T;
}

/**
 * Fail loudly when the live server disagrees with the recorded flags.
 *
 * Called once per page load against `tools/list`. Without it the recording is
 * a build-time guess that silently rots: a return annotation changing from
 * `dict` to `dict | None` flips the wrapping, and every view bound to that
 * tool would read `undefined` and render an empty state that looks like an
 * empty graph.
 */
export function assertFlagsMatchServer(
	live: { name: string; outputSchema?: Record<string, unknown> | null }[],
): void {
	const byName = new Map(live.map((tool) => [tool.name, tool]));
	const mismatches: string[] = [];

	for (const [name, recorded] of Object.entries(WRAPPED)) {
		const tool = byName.get(name);
		if (!tool) {
			mismatches.push(`${name} is missing from the server`);
			continue;
		}
		const actual = Boolean(tool.outputSchema?.["x-fastmcp-wrap-result"]);
		if (actual !== recorded) {
			mismatches.push(
				`${name} is ${actual ? "wrapped" : "unwrapped"} on the server but recorded as ${recorded ? "wrapped" : "unwrapped"}`,
			);
		}
	}

	if (mismatches.length > 0) {
		throw new WrapFlagMismatchError(mismatches);
	}
}
