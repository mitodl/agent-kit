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
	assert.match(text, /separate items/);
});

test("a path with a space is not mistaken for a folded option", () => {
	assert.equal(
		describeServerCommand(["/opt/my tools/witan", "lsp"]),
		'["/opt/my tools/witan","lsp"].',
	);
});
