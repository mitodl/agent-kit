import assert from "node:assert/strict";
import { test } from "node:test";
import {
	blockersOf,
	groupWaves,
	type ProjectWaves,
	type WaveTask,
	wavesNotes,
} from "../../src/waves.ts";

function row(slug: string, patch: Partial<WaveTask> = {}): WaveTask {
	return {
		slug,
		title: slug.toUpperCase(),
		status: "open",
		priority: "p2",
		wave: 0,
		outside: false,
		cycle: false,
		critical: false,
		downstream: 0,
		ready: true,
		open_blockers: [],
		...patch,
	};
}

// In the server's row order: by wave, outside blockers first.
const result: ProjectWaves = {
	repo: "https://github.com/test/repo",
	project: "wp-p",
	waves: 2,
	cycles: [],
	disagree: [],
	tasks: [
		row("tk-away", { outside: true, ready: false }),
		row("tk-a"),
		row("tk-b", { wave: 1, ready: false, open_blockers: ["tk-away", "tk-a"] }),
	],
};

test("the project's own tasks are grouped by wave, stubs left out", () => {
	assert.deepEqual(
		groupWaves(result).map((g) => [g.label, g.tasks.map((t) => t.slug)]),
		[
			["Wave 0 (ready)", ["tk-a"]],
			["Wave 1", ["tk-b"]],
		],
	);
});

test("a task's blockers include the one from another project", () => {
	const b = result.tasks[2] as WaveTask;
	assert.deepEqual(
		blockersOf(b, result).map((t) => [t.slug, t.outside]),
		[
			["tk-away", true],
			["tk-a", false],
		],
	);
});

test("a cycle and a disagreement with task_ready are named by title", () => {
	assert.deepEqual(wavesNotes(result), []);
	assert.deepEqual(
		wavesNotes({ ...result, cycles: [["tk-a", "tk-b"]], disagree: ["tk-a"] }),
		[
			"In a cycle, so none can become ready: TK-A → TK-B",
			"task_ready and the waves disagree on TK-A; refresh.",
		],
	);
});
