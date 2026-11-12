import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import wrapFlags from "../fixtures/tools-list.json" with { type: "json" };
import type { Memory } from "./types.js";

/**
 * A server whose `recall` ranks every pattern above the one lesson and honours
 * `limit`, as the real one does: the shape that hid a topic's lessons when the
 * Kind select narrowed after a limit at the page's row cap.
 */
const RANKED: Memory[] = [
	...Array.from({ length: 600 }, (_, i) => ({
		slug: `pat-${i}`,
		kind: "pattern",
	})),
	{ slug: "les-low", kind: "lesson" },
].map(
	(row) =>
		({
			...row,
			title: row.slug,
			content: "",
			repo: null,
			created_at: "2026-01-01T00:00:00+00:00",
		}) as unknown as Memory,
);
const TOPIC = {
	slug: "tp-topic-x",
	name: "x",
	kind: "topic",
	created_at: "2026-01-01T00:00:00+00:00",
};
const flags = wrapFlags as Record<string, { wrapped: boolean }>;

vi.mock("@modelcontextprotocol/client", () => ({
	Client: class {
		async connect() {}
		async listTools() {
			return {
				tools: Object.entries(flags).map(([name, info]) => ({
					name,
					inputSchema: { type: "object", properties: {} },
					outputSchema: info.wrapped ? { "x-fastmcp-wrap-result": true } : {},
				})),
			};
		}
		async callTool({
			name,
			arguments: args,
		}: {
			name: string;
			arguments: { limit: number };
		}) {
			if (name !== "recall") {
				throw new Error(`unexpected ${name}`);
			}
			const value = {
				memories: RANKED.slice(0, args.limit),
				contradictions: [],
				seeds: {},
				topic: TOPIC,
			};
			return {
				structuredContent: flags.recall?.wrapped ? { result: value } : value,
			};
		}
	},
	StreamableHTTPClientTransport: class {},
}));

beforeEach(() => {
	vi.stubGlobal(
		"fetch",
		vi.fn(async () => Response.json({ mcp_path: "/mcp", auth: null })),
	);
});

afterEach(async () => {
	(await import("./mcp.js")).resetClient();
	vi.unstubAllGlobals();
});

describe("topicGet", () => {
	it("reads past the row cap, so a kind filter sees a low-ranked lesson", async () => {
		const { topicGet } = await import("./mcp.js");
		const { TOPIC_ROW_CAP } = await import("./views/memory.js");

		const page = await topicGet(TOPIC.slug);

		expect(page?.memories.length).toBeGreaterThan(TOPIC_ROW_CAP);
		expect(page?.memories.filter((m) => m.kind === "lesson")).toHaveLength(1);
	});
});
