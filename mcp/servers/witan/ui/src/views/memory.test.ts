import { render } from "lit-html";
import { beforeEach, describe, expect, it, vi } from "vitest";
import contradictionsFixture from "../../fixtures/memory_contradictions.json" with {
	type: "json",
};
import memoryGetFixture from "../../fixtures/memory_get.json" with {
	type: "json",
};
import memoryListFixture from "../../fixtures/memory_list.json" with {
	type: "json",
};
import neighborsFixture from "../../fixtures/memory_neighbors.json" with {
	type: "json",
};
import supersededFixture from "../../fixtures/memory_neighbors.superseded.json" with {
	type: "json",
};
import recallFixture from "../../fixtures/recall.json" with { type: "json" };
import { DEFAULT_ROUTE, type Route } from "../route.js";
import type {
	Memory,
	MemoryContradiction,
	MemoryNeighbors,
	RecallResult,
} from "../types.js";
import { unwrap } from "../unwrap.js";
import {
	applyFacets,
	edgeMark,
	facetOptions,
	isMemorySlug,
	MEMORY_LIST_CAP,
	type MemoryPage,
	memoryDetail,
	memoryView,
} from "./memory.js";

const memory = unwrap<Memory>("memory_get", memoryGetFixture);
const listed = unwrap<Memory[]>("memory_list", memoryListFixture);
const neighbors = unwrap<MemoryNeighbors>("memory_neighbors", neighborsFixture);
const superseded = unwrap<MemoryNeighbors>(
	"memory_neighbors",
	supersededFixture,
);
const pairs = unwrap<MemoryContradiction[]>(
	"memory_contradictions",
	contradictionsFixture,
);
const recalled = unwrap<RecallResult>("recall", recallFixture);

const route: Route = { ...DEFAULT_ROUTE, view: "memory" };

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement("div");
	document.body.replaceChildren(root);
});

function text(): string {
	return root.textContent ?? "";
}

function draw(page: MemoryPage, at: Route = route, onNavigate = vi.fn()) {
	render(memoryView(page, at, onNavigate), root);
	return onNavigate;
}

describe("the contradictions inbox", () => {
	it("is the landing state, with both bodies side by side", () => {
		draw({ mode: "browse", memories: listed, inbox: pairs });

		const sides = root.querySelectorAll(".pair .side");
		expect(sides.length).toBe(2);
		const [pair] = pairs;
		expect(sides[0]?.textContent).toContain(pair?.a.title);
		expect(sides[1]?.textContent).toContain(pair?.b.title);
		// The bodies, from the pair itself: no read per side.
		expect(sides[0]?.textContent).toContain("x-fastmcp-wrap-result");
		expect(sides[1]?.textContent).toContain("returned 50 rows");
		expect(sides[0]?.textContent).toContain(pair?.a.author);
	});

	it("shows the link's confidence on every pair", () => {
		draw({ mode: "browse", memories: listed, inbox: pairs });

		expect(
			root.querySelector(".pair-head .confidence-asserted"),
		).not.toBeNull();
	});

	it("comes before the browse list", () => {
		draw({ mode: "browse", memories: listed, inbox: pairs });

		const inbox = root.querySelector(".inbox");
		const table = root.querySelector("table.memories");
		if (!inbox || !table) {
			throw new Error("the inbox or the list did not render");
		}
		expect(
			inbox.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING,
		).toBeTruthy();
	});

	it("says there are none rather than hiding the section", () => {
		draw({ mode: "browse", memories: listed, inbox: [] });

		expect(text()).toContain("No unresolved contradictions");
	});

	it("offers both supersede calls to resolve a pair", () => {
		draw({ mode: "browse", memories: listed, inbox: pairs });

		const [pair] = pairs;
		const code = root.querySelector(".resolve code")?.textContent ?? "";
		expect(code).toContain(
			`memory_link(from_slug="${pair?.a.slug}", to_slug="${pair?.b.slug}", kind="supersedes")`,
		);
		expect(code).toContain(
			`memory_link(from_slug="${pair?.b.slug}", to_slug="${pair?.a.slug}", kind="supersedes")`,
		);
	});
});

describe("the browse list's cap", () => {
	const full = Array.from({ length: MEMORY_LIST_CAP }, (_, i) => ({
		...memory,
		slug: `pat-capped-${i}`,
	}));

	it("says the list may be missing memories when it is full", () => {
		draw({ mode: "browse", memories: full, inbox: [] });

		expect(text()).toContain(`${MEMORY_LIST_CAP}-memory limit`);
	});

	it("says nothing when the list is shorter than the cap", () => {
		draw({ mode: "browse", memories: full.slice(1), inbox: [] });

		expect(root.querySelector("p.note")).toBeNull();
	});

	it("says nothing for a search, which the cap does not apply to", () => {
		draw({ mode: "plain", memories: full }, { ...route, q: "x", plain: true });

		expect(root.querySelector("p.note")).toBeNull();
	});
});

describe("recall's contradictions", () => {
	it("are reported for the same scope the inbox reads", () => {
		// ★ The acceptance criterion: what recall flags is in the inbox. Every
		// pair recall reports is an unordered pair `memory_contradictions` has.
		const inbox = new Set(
			pairs.map((pair) => [pair.a.slug, pair.b.slug].sort().join("|")),
		);
		expect(recalled.contradictions.length).toBeGreaterThan(0);
		for (const pair of recalled.contradictions) {
			expect(inbox).toContain([pair.a, pair.b].sort().join("|"));
		}
	});

	it("are listed, and marked on the rows, in recall mode", () => {
		draw(
			{
				mode: "recall",
				memories: recalled.memories,
				pairs: recalled.contradictions,
			},
			{ ...route, q: "unwrap" },
		);

		expect(root.querySelectorAll(".recall-pairs li").length).toBe(
			recalled.contradictions.length,
		);
		expect(root.querySelectorAll(".badge.contradicted").length).toBe(2);
	});

	it("are not drawn by the plain search, which does not compute them", () => {
		draw({ mode: "plain", memories: listed }, { ...route, q: "wrap" });

		expect(root.querySelector(".recall-pairs")).toBeNull();
		expect(root.querySelector(".inbox")).toBeNull();
	});
});

describe("facets", () => {
	const lesson: Memory = {
		...memory,
		slug: "les-x",
		tags: ["other"],
		author: "someone",
		language: "python",
	};

	it("narrow to memories with every set value", () => {
		expect(
			applyFacets([memory, lesson], { ...DEFAULT_ROUTE.facets, tag: "mcp" }),
		).toEqual([memory]);
		expect(
			applyFacets([memory, lesson], {
				...DEFAULT_ROUTE.facets,
				author: "someone",
				language: "python",
			}),
		).toEqual([lesson]);
	});

	it("offer every value in the read, not only the filtered ones", () => {
		const options = facetOptions([memory, lesson]);

		expect(options.tag).toEqual(["mcp", "other", "witan-ui"]);
		expect(options.author).toEqual(["fixtures", "someone"]);
		expect(options.severity).toEqual([]);
	});

	it("draw no select for a facet with no values", () => {
		draw({ mode: "plain", memories: [memory] }, { ...route, q: "x" });

		const labels = [...root.querySelectorAll(".memory-toolbar label")].map(
			(l) => l.textContent?.trim().split(/\s/)[0],
		);
		expect(labels).toContain("Tag");
		expect(labels).not.toContain("Severity");
	});

	it("keep a set value clearable when the read no longer has it", () => {
		draw(
			{ mode: "plain", memories: [memory] },
			{
				...route,
				q: "x",
				facets: { ...DEFAULT_ROUTE.facets, severity: "critical" },
			},
		);

		expect(text()).toContain("critical");
		expect(text()).toContain("No memories match these filters.");
	});

	it("navigate through the route when changed", () => {
		const onNavigate = draw(
			{ mode: "plain", memories: [memory] },
			{ ...route, q: "x" },
		);
		const select = [...root.querySelectorAll("select")].find((s) =>
			s.closest("label")?.textContent?.includes("Tag"),
		);
		if (!select) {
			throw new Error("no tag select");
		}
		select.value = "mcp";
		select.dispatchEvent(new Event("change"));

		expect(onNavigate).toHaveBeenCalledWith({
			facets: { ...DEFAULT_ROUTE.facets, tag: "mcp" },
		});
	});
});

describe("the search form", () => {
	it("submits the query and leaves a topic", () => {
		const onNavigate = draw(
			{ mode: "topic", topic: null, memories: [] },
			{ ...route, topic: "tp-x" },
		);
		const input = root.querySelector<HTMLInputElement>("input[name=q]");
		const form = root.querySelector("form");
		if (!input || !form) {
			throw new Error("no search form");
		}
		input.value = "  wrap flag ";
		form.dispatchEvent(new SubmitEvent("submit", { cancelable: true }));

		expect(onNavigate).toHaveBeenCalledWith({ q: "wrap flag", topic: null });
	});
});

describe("the topic list", () => {
	it("names a missing topic as a stale link", () => {
		draw(
			{ mode: "topic", topic: null, memories: [] },
			{ ...route, topic: "tp-gone" },
		);

		expect(text()).toContain("No topic tp-gone");
		expect(root.querySelector("table")).toBeNull();
	});

	it("offers no control topic_get cannot honour, and no column it does not send", () => {
		draw(
			{
				mode: "topic",
				topic: {
					slug: "tp-topic-mcp",
					name: "mcp",
					kind: "topic",
					created_at: memory.created_at,
				},
				memories: [memory],
			},
			{ ...route, topic: "tp-topic-mcp" },
		);

		expect(text()).not.toContain("Show superseded");
		const headings = [...root.querySelectorAll("th")].map(
			(th) => th.textContent,
		);
		expect(headings).not.toContain("Author");
		expect(headings).not.toContain("Tags");
	});
});

describe("memoryDetail", () => {
	function panel(n: MemoryNeighbors = neighbors, m: Memory = memory) {
		render(memoryDetail({ memory: m, neighbors: n }, route), root);
	}

	it("shows the memory's own fields and body", () => {
		panel();

		expect(text()).toContain(memory.slug);
		expect(text()).toContain("x-fastmcp-wrap-result");
		expect(text()).toContain("mcp, witan-ui");
	});

	it("groups neighbours by edge kind, with confidence on every one", () => {
		panel();

		const groups = [...root.querySelectorAll(".edges h3")].map((h) =>
			// Without the count badge.
			h.textContent?.replace(/\d+\s*$/, "").trim(),
		);
		expect(groups).toEqual(["Topics", "Supersedes", "Contradicts"]);

		const rows = root.querySelectorAll("ul.neighbors li");
		const marked = root.querySelectorAll(
			"ul.neighbors li .edge-meta [class*=confidence-]",
		);
		expect(rows.length).toBe(4);
		expect(marked.length).toBe(rows.length);
	});

	it("draws inferred and asserted edges differently", () => {
		// ★ recall weights them differently, and a topic promoted from a
		// free-string tag is a much weaker claim than a link someone named.
		panel();

		const inferred = root.querySelectorAll("li.edge-inferred");
		const asserted = root.querySelectorAll("li.edge-asserted");
		expect(inferred.length).toBe(memory.topics?.length);
		expect(asserted.length).toBe(2);
		expect(inferred[0]?.textContent).toContain("inferred");
	});

	it("shows the link's role", () => {
		panel();

		expect(text()).toContain("“the shape test misreads a bare dict”");
	});

	it("links a topic to that topic's list", () => {
		panel();

		const link = root.querySelector<HTMLAnchorElement>(".edge-inferred a");
		expect(link?.getAttribute("href")).toBe("#memory?topic=tp-topic-witan-ui");
	});

	it("drops the facets when following a topic, whose rows have none", () => {
		render(
			memoryDetail(
				{ memory, neighbors },
				{ ...route, facets: { ...DEFAULT_ROUTE.facets, language: "python" } },
			),
			root,
		);

		const link = root.querySelector<HTMLAnchorElement>(".edge-inferred a");
		expect(link?.getAttribute("href")).toBe("#memory?topic=tp-topic-witan-ui");
	});

	it("renders a memory with no edges without empty scaffolding", () => {
		const bare: Memory = { ...memory, topics: [], symbol_refs: null };
		const none: MemoryNeighbors = {
			slug: memory.slug,
			neighbors: Object.fromEntries(
				Object.keys(neighbors.neighbors).map((kind) => [kind, []]),
			),
		};
		panel(none, bare);

		expect(root.querySelectorAll("section.edges").length).toBe(0);
		expect(root.querySelectorAll("h3").length).toBe(0);
		expect(text()).toContain("x-fastmcp-wrap-result");
	});

	it("says what replaced a superseded memory", () => {
		panel(superseded, { ...memory, slug: superseded.slug, topics: [] });

		expect(root.querySelector(".badge.superseded")).not.toBeNull();
		const note = root.querySelector(".superseded-note");
		expect(note?.textContent).toContain(memory.title);
		expect(note?.querySelector("a")?.getAttribute("href")).toBe(
			`#memory?slug=${memory.slug}`,
		);
	});
});

describe("edgeMark", () => {
	it("names an edge from before provenance as unrecorded, not asserted", () => {
		render(
			edgeMark({
				confidence: null,
				role: null,
				author: null,
				created_at: null,
			}),
			root,
		);

		expect(text().trim()).toBe("unrecorded");
	});
});

describe("isMemorySlug", () => {
	it("knows every memory prefix witan mints, and not a task's", () => {
		for (const slug of ["pat-a", "pf-a", "les-a", "ctx-a", "mem-a"]) {
			expect(isMemorySlug(slug)).toBe(true);
		}
		expect(isMemorySlug("tk-a")).toBe(false);
		expect(isMemorySlug("wp-a")).toBe(false);
	});
});
