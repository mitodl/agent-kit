import { describe, expect, it } from "vitest";
import {
	isInterfaceBinding,
	isMemory,
	isMemoryContradiction,
	isMemoryNeighbors,
	isProjectStatus,
	isRecallResult,
	isRepoDependencies,
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
 * Checking only the MCP envelope was not enough: a renamed field in a result
 * nothing else guarded (`task_search`, `workflow_project_get`/`list`,
 * `workflow_session_list`, `topic_get`) survived a regeneration and left the
 * hand-written type quietly stale.
 *
 * ★ EXHAUSTIVE OVER TOOLS AND OVER FIELDS, and both halves are pinned here
 * rather than asserted in a comment. `has a validator for every bound tool`
 * pins the first: a tool cannot be added without a guard. `notices the loss of
 * every required field` pins the second, by dropping each key of each fixture
 * in turn and requiring the validator to reject it unless the type declares
 * that field optional. An earlier version checked only the fields that
 * DISCRIMINATE one projection from another, which let a rename of any other
 * field pass in the same PR that regenerated the fixture.
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
	memory_contradictions: (v) =>
		Array.isArray(v) && v.every(isMemoryContradiction),
	topic_get: (v) => v === null || isTopicResult(v),
	code_repo_dependencies: isRepoDependencies,
	code_interface_providers: (v) =>
		Array.isArray(v) && v.every(isInterfaceBinding),
	code_interface_consumers: (v) =>
		Array.isArray(v) && v.every(isInterfaceBinding),
};

/**
 * What each tool's type declares OPTIONAL: `?`-marked fields whose absence is
 * legal, so dropping one is not a break. Everything else a fixture carries is
 * required, and the sweep below requires the validator to notice its loss.
 *
 * This is the list that has to be maintained, and that is the point: adding an
 * optional field to a type without teaching its guard the field's type fails
 * the sweep, because the drop goes unnoticed and the key is not listed here.
 */
const MEMORY_OPTIONAL = [
	"content",
	"tags",
	"author",
	"confidence",
	"category",
	"severity",
	"language",
	"symbol_refs",
	"updated_at",
];
const OPTIONAL_FIELDS: Record<string, string[]> = {
	// `TaskCore.lease_expired`: present only on an `in_progress` row.
	task_ready: ["lease_expired"],
	task_get: ["lease_expired"],
	task_list: ["lease_expired"],
	task_search: ["lease_expired"],
	workflow_project_get: [],
	workflow_project_status: [],
	workflow_project_list: [],
	workflow_session_list: ["session_id", "tools_used", "files_changed"],
	recall: [],
	// `topics` only with `include_topics`, which the UI always passes.
	memory_get: [...MEMORY_OPTIONAL, "topics"],
	memory_list: MEMORY_OPTIONAL,
	memory_search: MEMORY_OPTIONAL,
	memory_neighbors: [],
	memory_contradictions: [],
	topic_get: [],
	code_repo_dependencies: [],
	code_interface_providers: [],
	code_interface_consumers: [],
};

/** `../fixtures/task_get.missing.json` -> `task_get`. */
function toolOf(path: string): string {
	const base = path.split("/").pop() ?? "";
	return base.replace(/\.json$/, "").split(".")[0] ?? "";
}

/** Fixtures that record something other than a tool result. */
const NOT_TOOL_RESULTS = ["tools-list.json", "repo-keys.json", "graph.json"];

const TOOL_FIXTURES = Object.entries(FIXTURES).filter(
	([path]) => !NOT_TOOL_RESULTS.some((name) => path.endsWith(name)),
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
	)("notices the loss of every required field in %s", (path, payload) => {
		const tool = toolOf(path);
		const validate = VALIDATORS[tool];
		const value = unwrap(tool, payload);
		// `task_get.missing.json` is `null` and `task_list.empty.json` is `[]`:
		// real results, with no record to take a field away from.
		const first = Array.isArray(value) ? value[0] : value;
		if (first === null || typeof first !== "object") {
			return;
		}

		const optional = OPTIONAL_FIELDS[tool] ?? [];
		const unnoticed = Object.keys(first).filter((key) => {
			const mutated = structuredClone(value);
			const target = (Array.isArray(mutated) ? mutated[0] : mutated) as Record<
				string,
				unknown
			>;
			delete target[key];
			return validate?.(mutated) === true;
		});

		// Subset, not equality: a fixture carries an optional field only when
		// the recorded row happened to have one, so `lease_expired` is absent
		// from every fixture with no `in_progress` row.
		expect(unnoticed.filter((key) => !optional.includes(key))).toEqual([]);
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
