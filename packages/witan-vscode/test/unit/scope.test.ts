import assert from "node:assert/strict";
import { test } from "node:test";
import {
	appliedLabel,
	DEFAULT_SCOPE,
	projectChoices,
	requested,
	restoreScope,
	scopeArgs,
} from "../../src/scope.ts";

const ONE = "file:///work/one";
const TWO = "file:///work/two";
const ALPHA = { project: "wp-a", title: "Alpha", folder: ONE };

test("a repo scope is sent as scope, a project as project alone", () => {
	assert.deepEqual(scopeArgs({ scope: "all" }), { scope: "all" });
	assert.deepEqual(scopeArgs(ALPHA), { project: "wp-a" });
});

test("the label is what the server applied", () => {
	assert.equal(
		appliedLabel({ scope: "repo", project: null }, DEFAULT_SCOPE),
		"This repo",
	);
	assert.equal(
		appliedLabel({ scope: "project", project: "wp-a" }, ALPHA),
		"Project: Alpha",
	);
	// Another project than the one picked is named by its slug, not mislabeled.
	assert.equal(
		appliedLabel({ scope: "project", project: "wp-b" }, ALPHA),
		"Project: wp-b",
	);
});

test("before a list answers, the label is the scope picked", () => {
	const alpha = ALPHA;
	assert.equal(appliedLabel(requested(alpha), alpha), "Project: Alpha");
	const all = { scope: "all" } as const;
	assert.equal(appliedLabel(requested(all), all), "All repos");
});

test("a server older than scopes is labeled with what it lists", () => {
	assert.equal(appliedLabel({}, { scope: "all" }), "This repo + unfiled");
});

test("stored state that is not a scope restores the default", () => {
	for (const stored of [
		undefined,
		null,
		"repo",
		{ scope: "everything" },
		{},
		// A project with no folder to route it through.
		{ project: "wp-a", title: "A" },
	]) {
		assert.deepEqual(restoreScope(stored), DEFAULT_SCOPE);
	}
	assert.deepEqual(restoreScope({ scope: "repo+unfiled" }), {
		scope: "repo+unfiled",
	});
	assert.deepEqual(restoreScope(ALPHA), ALPHA);
});

test("each folder's projects are offered once, with the folder that listed them", () => {
	const a = { slug: "wp-a", title: "A" };
	const b = { slug: "wp-b", title: "B" };
	assert.deepEqual(
		projectChoices([
			{ folder: ONE, projects: [a] },
			// Another graph's project, and one a shared graph lists again.
			{ folder: TWO, projects: [a, b] },
		]),
		[
			{ ...a, folder: ONE },
			{ ...b, folder: TWO },
		],
	);
});
