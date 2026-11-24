import assert from "node:assert/strict";
import { test } from "node:test";
import {
	appliedLabel,
	DEFAULT_SCOPE,
	restoreScope,
	scopeArgs,
} from "../../src/scope.ts";

test("a repo scope is sent as scope, a project as project alone", () => {
	assert.deepEqual(scopeArgs({ scope: "all" }), { scope: "all" });
	assert.deepEqual(scopeArgs({ project: "wp-a", title: "A" }), {
		project: "wp-a",
	});
});

test("the label is what the server applied", () => {
	assert.equal(
		appliedLabel({ scope: "repo", project: null }, DEFAULT_SCOPE),
		"This repo",
	);
	assert.equal(
		appliedLabel(
			{ scope: "project", project: "wp-a" },
			{ project: "wp-a", title: "Alpha" },
		),
		"Project: Alpha",
	);
	// Another project than the one picked is named by its slug, not mislabeled.
	assert.equal(
		appliedLabel(
			{ scope: "project", project: "wp-b" },
			{ project: "wp-a", title: "Alpha" },
		),
		"Project: wp-b",
	);
});

test("a server older than scopes is labeled with what it lists", () => {
	assert.equal(appliedLabel({}, { scope: "all" }), "This repo + unfiled");
});

test("stored state that is not a scope restores the default", () => {
	for (const stored of [undefined, null, "repo", { scope: "everything" }, {}]) {
		assert.deepEqual(restoreScope(stored), DEFAULT_SCOPE);
	}
	assert.deepEqual(restoreScope({ scope: "repo+unfiled" }), {
		scope: "repo+unfiled",
	});
	assert.deepEqual(restoreScope({ project: "wp-a", title: "A" }), {
		project: "wp-a",
		title: "A",
	});
});
