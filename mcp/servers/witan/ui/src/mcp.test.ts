import { describe, expect, it } from "vitest";
import recallFixture from "../fixtures/recall.json" with { type: "json" };
import recallMemoryFixture from "../fixtures/recall.memory.json" with {
	type: "json",
};
import recallTopicFixture from "../fixtures/recall.topic.json" with {
	type: "json",
};
import { memorySectionOf, scoped, topicResultOf, withInRepo } from "./mcp.js";
import type { RecallResult } from "./types.js";
import { unwrap } from "./unwrap.js";

describe("recall's seed sections", () => {
	const plain = unwrap<RecallResult>("recall", recallFixture);

	it("reads a topic page from the topic section", () => {
		const result = unwrap<RecallResult>("recall", recallTopicFixture);
		const page = topicResultOf(result);

		expect(page?.topic.slug).toBe(result.topic?.slug);
		expect(page?.memories).toEqual(result.memories);
	});

	it("reads a missing topic as null", () => {
		expect(topicResultOf({ ...plain, topic: null })).toBeNull();
	});

	it("reads a memory's links from the memory section", () => {
		const result = unwrap<RecallResult>("recall", recallMemoryFixture);

		expect(memorySectionOf(result)).toBe(result.memory);
	});

	it("refuses a result with no memory section", () => {
		expect(() => memorySectionOf(plain)).toThrow("no memory section");
	});
});

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

describe("scoped", () => {
	it("sends every repo as all_repos, never an empty repo the tools refuse", () => {
		expect(scoped({ repo: "", limit: 5 })).toEqual({
			all_repos: true,
			limit: 5,
		});
	});

	it("sends a chosen repo as repo", () => {
		const repo = "https://github.com/example/web";
		expect(scoped({ repo, limit: 5 })).toEqual({ repo, limit: 5 });
	});
});
