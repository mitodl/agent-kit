import assert from "node:assert/strict";
import { test } from "node:test";
import { describeServerCommand } from "../../src/serverCommand.ts";

test("the server command shows each argument as written", () => {
	assert.equal(
		describeServerCommand(["witan", "lsp", "--target", "production"]),
		'["witan","lsp","--target","production"].',
	);
});

test("an option folded together with its value is named, with the fix", () => {
	const text = describeServerCommand(["witan", "lsp", "--target production"]);
	// A space-joined rendering would read `witan lsp --target production`.
	assert.match(text, /^\["witan","lsp","--target production"\]\./);
	assert.match(
		text,
		/"--target production" reaches the server as a single argument/,
	);
	assert.match(text, /one item per argument/);
});

test("a fold anywhere in an item is named, not only at its start", () => {
	for (const arg of ["lsp --target production", " --target production"]) {
		assert.match(
			describeServerCommand(["witan", arg]),
			/reaches the server as a single argument/,
			arg,
		);
	}
});

test("a path with a space, or one --opt=value argument, is not flagged", () => {
	for (const argv of [
		["/opt/my tools/witan", "lsp"],
		["witan", "lsp", "--target=my target"],
	]) {
		assert.equal(describeServerCommand(argv), `${JSON.stringify(argv)}.`);
	}
});
