import { describe, expect, it } from "vitest";
import { withInRepo } from "./mcp.js";

describe("withInRepo", () => {
	const args = {
		kind: "env_var" as const,
		key: "DATABASE_URL",
		in_repo: "https://github.com/example/web",
	};

	it("sends in_repo to a server whose tool declares it", () => {
		expect(withInRepo(args, new Set(["kind", "key", "in_repo"]))).toEqual(args);
	});

	it("leaves it off for an older witan-code, which would reject the call", () => {
		expect(withInRepo(args, new Set(["kind", "key"]))).toEqual({
			kind: "env_var",
			key: "DATABASE_URL",
		});
	});

	it("leaves it off before the tool list has been read", () => {
		expect(withInRepo(args, undefined)).not.toHaveProperty("in_repo");
	});
});
