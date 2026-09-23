import { describe, expect, it } from "vitest";
import memoryContradictionsFixture from "../fixtures/memory_contradictions.json" with {
	type: "json",
};
import memoryGetFixture from "../fixtures/memory_get.json" with {
	type: "json",
};
import memoryListFixture from "../fixtures/memory_list.json" with {
	type: "json",
};
import memoryNeighborsFixture from "../fixtures/memory_neighbors.json" with {
	type: "json",
};
import recallFixture from "../fixtures/recall.json" with { type: "json" };
import taskGetFixture from "../fixtures/task_get.json" with { type: "json" };
import taskGetMissingFixture from "../fixtures/task_get.missing.json" with {
	type: "json",
};
import taskListEmptyFixture from "../fixtures/task_list.empty.json" with {
	type: "json",
};
import taskListFixture from "../fixtures/task_list.json" with { type: "json" };
import taskReadyFixture from "../fixtures/task_ready.json" with {
	type: "json",
};
import projectStatusFixture from "../fixtures/workflow_project_status.json" with {
	type: "json",
};
import {
	isMemory,
	isMemoryContradiction,
	isProjectStatus,
	isRecallResult,
	isTaskDetail,
	isTaskRow,
	type MemoryContradiction,
	type ProjectStatus,
	type RecallResult,
	type TaskDetail,
	type TaskRow,
} from "./types.js";
import {
	assertFlagsMatchServer,
	BOUND_TOOLS,
	OPTIONAL_TOOLS,
	UnknownToolError,
	unwrap,
	WrapFlagMismatchError,
} from "./unwrap.js";

/**
 * ★ THESE FIXTURES ARE REAL TOOL RESULTS, recorded by `just ui-fixtures`
 * against a seeded store. That is what makes this suite a contract test
 * rather than a test of types against themselves: a server change that
 * renames a field, or flips whether a result is wrapped, fails here in the
 * same PR that made it.
 */

/** The tools unwrapped on the server: each returns a bare `dict`. */
const UNWRAPPED = new Set([
	"recall",
	"memory_neighbors",
	"code_repo_dependencies",
]);

/** A `tools/list` that agrees with every recorded flag. */
function agreeing(): {
	name: string;
	outputSchema: Record<string, unknown>;
}[] {
	return BOUND_TOOLS.map((name) => ({
		name,
		outputSchema: UNWRAPPED.has(name) ? {} : { "x-fastmcp-wrap-result": true },
	}));
}

describe("unwrap", () => {
	it("takes a wrapped list out of its envelope", () => {
		const rows = unwrap<TaskRow[]>("task_list", taskListFixture);

		expect(Array.isArray(rows)).toBe(true);
		expect(rows.length).toBeGreaterThan(0);
		expect(rows.every(isTaskRow)).toBe(true);
	});

	it("passes a bare dict through untouched", () => {
		// `recall` returns a plain dict, so fastmcp does not wrap it. Guessing
		// from the value's shape would look for a `result` key and find none.
		const result = unwrap<RecallResult>("recall", recallFixture);

		expect(isRecallResult(result)).toBe(true);
		expect(result).toBe(recallFixture);
	});

	it("reads a wrapped null as null, not as an absent value", () => {
		// ★ `task_get` of a slug that does not exist. A page that treats this as
		// an error shows a crash for a stale link, which is exactly what
		// discovery found the CLI doing.
		expect(unwrap("task_get", taskGetMissingFixture)).toBeNull();
	});

	it("reads a wrapped empty list as an empty list", () => {
		expect(unwrap("task_list", taskListEmptyFixture)).toEqual([]);
	});

	it("refuses a tool outside the bound set", () => {
		expect(() => unwrap("task_close", { result: null })).toThrow(
			UnknownToolError,
		);
	});

	it("refuses a wrapped tool whose result has no result key", () => {
		expect(() => unwrap("task_list", { rows: [] })).toThrow(/no "result" key/);
	});
});

describe("the recorded wrap flags", () => {
	it("cover exactly ADR 0011's bound set", () => {
		expect(BOUND_TOOLS).toEqual([
			"code_interface_consumers",
			"code_interface_providers",
			"code_repo_dependencies",
			"memory_contradictions",
			"memory_get",
			"memory_list",
			"memory_neighbors",
			"memory_search",
			"recall",
			"task_get",
			"task_list",
			"task_ready",
			"task_search",
			"topic_get",
			"workflow_project_get",
			"workflow_project_list",
			"workflow_project_status",
			"workflow_session_list",
		]);
	});

	it("accept a server that agrees with them", () => {
		expect(() => assertFlagsMatchServer(agreeing())).not.toThrow();
	});

	it("record exactly witan-code's three tools as optional", () => {
		expect([...OPTIONAL_TOOLS].sort()).toEqual([
			"code_interface_consumers",
			"code_interface_providers",
			"code_repo_dependencies",
		]);
	});

	it("accept a server without the optional code-graph tools", () => {
		// witan-code is mounted only when installed (ADR 0011, 2026-09-23
		// amendment), so its absence is a configuration, not a mismatch.
		const live = agreeing().filter((tool) => !OPTIONAL_TOOLS.has(tool.name));

		expect(() => assertFlagsMatchServer(live)).not.toThrow();
	});

	it("still check an optional tool's wrapping when it is present", () => {
		const live = agreeing().map((tool) =>
			tool.name === "code_repo_dependencies"
				? { ...tool, outputSchema: { "x-fastmcp-wrap-result": true } }
				: tool,
		);

		expect(() => assertFlagsMatchServer(live)).toThrow(
			/code_repo_dependencies/,
		);
	});

	it("reject a server that has flipped exactly one", () => {
		// The failure this exists for: a return annotation changing from `dict`
		// to `dict | None` flips the wrapping, and every view bound to that tool
		// would read `undefined` and render an empty state that looks like an
		// empty graph.
		// ONE tool changed, with every other flag correct: flipping them all
		// would throw for a broader reason than the name claims.
		const live = agreeing().map((tool) =>
			tool.name === "recall"
				? { ...tool, outputSchema: { "x-fastmcp-wrap-result": true } }
				: tool,
		);

		expect(() => assertFlagsMatchServer(live)).toThrow(WrapFlagMismatchError);
		expect(() => assertFlagsMatchServer(live)).toThrow(/recall/);
	});

	it("reject a server missing a bound tool", () => {
		const live = BOUND_TOOLS.filter((name) => name !== "task_ready").map(
			(name) => ({
				name,
				outputSchema: { "x-fastmcp-wrap-result": true },
			}),
		);

		expect(() => assertFlagsMatchServer(live)).toThrow(/task_ready is missing/);
	});
});

describe("the types match what the server actually returns", () => {
	it("task_get carries its edges and its comments", () => {
		const task = unwrap<TaskDetail>("task_get", taskGetFixture);

		expect(isTaskDetail(task)).toBe(true);
		// The edges themselves, not just their arrayness: `isTaskDetail` already
		// asserted that, so re-checking it would pass on three empty lists.
		expect(task.blocks.length).toBeGreaterThan(0);
		expect(typeof task.blocks[0]).toBe("string");
		expect(task.comments.length).toBeGreaterThan(0);
		expect(typeof task.comments[0]?.body).toBe("string");
	});

	it("list rows carry the timestamps a lifetime chart needs", () => {
		const rows = unwrap<TaskRow[]>("task_list", taskListFixture);

		for (const row of rows) {
			expect(typeof row.created_at).toBe("string");
			expect(row).toHaveProperty("closed_at");
		}
	});

	it("only an in_progress row carries lease_expired", () => {
		// The board reads this to mark a stale claim. On any other status a
		// `false` would read as "claim still live", so absence is the contract.
		const rows = unwrap<TaskRow[]>("task_list", taskListFixture);

		for (const row of rows) {
			if (row.status === "in_progress") {
				expect(typeof row.lease_expired).toBe("boolean");
			} else {
				expect(row.lease_expired).toBeUndefined();
			}
		}
	});

	it("task_ready rows are task rows", () => {
		expect(
			unwrap<TaskRow[]>("task_ready", taskReadyFixture).every(isTaskRow),
		).toBe(true);
	});

	it("the project rollup counts every ready task, not just the listed ones", () => {
		const status = unwrap<ProjectStatus>(
			"workflow_project_status",
			projectStatusFixture,
		);

		expect(isProjectStatus(status)).toBe(true);
		// The invariant the read-gap change introduced: the count is taken
		// before the truncation, so it is >= the listed rows, and
		// `ready_truncated` says exactly when the two differ.
		expect(status.counts.ready).toBeGreaterThanOrEqual(
			status.ready_tasks.length,
		);
		expect(status.ready_truncated).toBe(
			status.counts.ready > status.ready_tasks.length,
		);
	});

	it("memory reads are memories", () => {
		expect(isMemory(unwrap("memory_get", memoryGetFixture))).toBe(true);
		expect(
			unwrap<unknown[]>("memory_list", memoryListFixture).every(isMemory),
		).toBe(true);
	});

	it("memory_neighbors is a bare dict keyed by slug", () => {
		const neighbors = unwrap<{ slug: string }>(
			"memory_neighbors",
			memoryNeighborsFixture,
		);

		expect(typeof neighbors.slug).toBe("string");
	});

	it("memory_contradictions carries both endpoints and the link", () => {
		const pairs = unwrap<MemoryContradiction[]>(
			"memory_contradictions",
			memoryContradictionsFixture,
		);

		// The seed links exactly one pair `contradicts`.
		expect(pairs).toHaveLength(1);
		expect(pairs.every(isMemoryContradiction)).toBe(true);
		const [pair] = pairs;
		expect(pair?.a.slug).not.toBe(pair?.b.slug);
		expect(pair?.edge.confidence).toBe("asserted");
	});

	it("recall reports contradictions as unordered pairs", () => {
		const result = unwrap<RecallResult>("recall", recallFixture);

		expect(isRecallResult(result)).toBe(true);
		// The seed links two memories `contradicts`, so an empty list here means
		// recall stopped reporting them rather than that there are none.
		expect(result.contradictions.length).toBeGreaterThan(0);
		for (const pair of result.contradictions) {
			expect(typeof pair.a).toBe("string");
			expect(typeof pair.b).toBe("string");
		}
	});
});
