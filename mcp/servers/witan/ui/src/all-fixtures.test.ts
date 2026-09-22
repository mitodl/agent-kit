import { describe, expect, it } from "vitest";
import {
	isMemory,
	isMemoryNeighbors,
	isProjectStatus,
	isRecallResult,
	isTaskDetail,
	isTaskRow,
	isTaskSearchRow,
	isTopicResult,
	isWorkflowProjectDetail,
	isWorkflowProjectSummary,
	isWorkflowSession,
} from "./types.js";
import { BOUND_TOOLS, unwrap } from "./unwrap.js";

/**
 * ★ EVERY FIXTURE, AND EVERY FIXTURE'S UNWRAPPED VALUE.
 *
 * `fixtures.test.ts` asserts the specific shapes the views lean on. This file
 * walks the whole directory, so a fixture added by `just ui-fixtures` is
 * covered the moment it lands rather than when someone remembers to import
 * it.
 *
 * Checking only the MCP envelope was not enough, and that gap was real: a
 * renamed field in a result nothing else guarded (`task_search`,
 * `workflow_project_get`/`list`, `workflow_session_list`, `topic_get`) would
 * survive a regeneration and leave the hand-written type quietly stale, which
 * is the one thing these fixtures exist to prevent. VALIDATORS below is
 * exhaustive over the bound set, and the test that asserts so is what stops a
 * new tool from being added without one.
 */
const FIXTURES = import.meta.glob("../fixtures/*.json", {
	eager: true,
	import: "default",
}) as Record<string, unknown>;

/** One guard per bound tool, applied to the unwrapped value. */
const VALIDATORS: Record<string, (value: unknown) => boolean> = {
	task_ready: (v) => Array.isArray(v) && v.every(isTaskRow),
	// `null` is a real result here: a slug that does not exist.
	task_get: (v) => v === null || isTaskDetail(v),
	task_list: (v) => Array.isArray(v) && v.every(isTaskRow),
	task_search: (v) => Array.isArray(v) && v.every(isTaskSearchRow),
	workflow_project_get: (v) => v === null || isWorkflowProjectDetail(v),
	workflow_project_status: (v) => v === null || isProjectStatus(v),
	workflow_project_list: (v) =>
		Array.isArray(v) && v.every(isWorkflowProjectSummary),
	workflow_session_list: (v) => Array.isArray(v) && v.every(isWorkflowSession),
	recall: isRecallResult,
	memory_get: (v) => v === null || isMemory(v),
	memory_list: (v) => Array.isArray(v) && v.every(isMemory),
	memory_search: (v) => Array.isArray(v) && v.every(isMemory),
	memory_neighbors: isMemoryNeighbors,
	topic_get: (v) => v === null || isTopicResult(v),
};

/** `../fixtures/task_get.missing.json` -> `task_get`. */
function toolOf(path: string): string {
	const base = path.split("/").pop() ?? "";
	return base.replace(/\.json$/, "").split(".")[0] ?? "";
}

const TOOL_FIXTURES = Object.entries(FIXTURES).filter(
	([path]) => !path.endsWith("tools-list.json"),
);

describe("every recorded fixture", () => {
	it("covers each bound tool at least once", () => {
		const covered = new Set(TOOL_FIXTURES.map(([path]) => toolOf(path)));

		expect([...covered].sort()).toEqual(BOUND_TOOLS);
	});

	it("has a validator for every bound tool", () => {
		// Without this, adding a tool and forgetting its guard would leave the
		// walk below silently checking nothing for it.
		expect(Object.keys(VALIDATORS).sort()).toEqual(BOUND_TOOLS);
	});

	it.each(TOOL_FIXTURES)("unwraps and validates %s", (path, payload) => {
		const tool = toolOf(path);

		// `unwrap` throws for an unknown tool and for a wrapped result with no
		// `result` key, so a fixture recorded against a server whose wrapping
		// changed fails here.
		const value = unwrap(tool, payload);

		const validator = VALIDATORS[tool];
		expect(validator, `no validator for ${tool}`).toBeDefined();
		expect(validator?.(value), `${tool} no longer matches its type`).toBe(true);
	});

	it.each(
		TOOL_FIXTURES,
	)("records %s as JSON the page can hold", (_path, payload) => {
		// Nothing that survived `default=str` as a repr: a value like
		// "datetime.datetime(...)" means the generator coerced past its own
		// normalizer, which would make the gate flake.
		const text = JSON.stringify(payload);
		expect(text).not.toContain("datetime.datetime");
		expect(text).not.toContain("<");
	});
});
