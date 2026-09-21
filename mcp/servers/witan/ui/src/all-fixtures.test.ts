import { describe, expect, it } from "vitest";
import { BOUND_TOOLS, unwrap } from "./unwrap.js";

/**
 * ★ EVERY FIXTURE, NOT A HAND-PICKED SUBSET.
 *
 * `fixtures.test.ts` asserts specific shapes for the tools whose results the
 * views lean on. This file is the other half: it walks the whole directory,
 * so a fixture added by `just ui-fixtures` is covered the moment it lands
 * rather than when someone remembers to import it.
 *
 * That distinction was not academic. An earlier version of this suite imported
 * 10 of 17 fixtures while four places claimed it parsed all of them, and the
 * two left out were exactly the two whose recorded rows did not match their
 * declared types (`task_search` and `workflow_project_list` are narrower
 * projections than the list tools).
 *
 * `import.meta.glob` is Vite's, eager so the fixtures are inlined at build
 * time rather than fetched: this runs under vitest with no server.
 */
const FIXTURES = import.meta.glob("../fixtures/*.json", {
	eager: true,
	import: "default",
}) as Record<string, unknown>;

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

	it.each(TOOL_FIXTURES)("unwraps %s", (path, payload) => {
		const tool = toolOf(path);

		// The real assertion: `unwrap` throws for a tool it does not know and for
		// a wrapped result with no `result` key, so a fixture recorded against a
		// server whose wrapping has changed fails right here.
		const value = unwrap(tool, payload);

		expect(value === null || value !== undefined).toBe(true);
	});

	it.each(
		TOOL_FIXTURES,
	)("records %s as JSON the page can hold", (_path, payload) => {
		// No `undefined`, no NaN, nothing that survived `default=str` as a repr.
		// A value like "datetime.datetime(...)" in a fixture means the generator
		// coerced past its own normalizer, which would make the gate flake.
		const text = JSON.stringify(payload);
		expect(text).not.toContain("datetime.datetime");
		expect(text).not.toContain("<");
	});
});
