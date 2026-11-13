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
import staleRefsFixture from "../../fixtures/memory_stale_refs.json" with {
	type: "json",
};
import recallFixture from "../../fixtures/recall.json" with { type: "json" };
import neighborsFixture from "../../fixtures/recall.memory.json" with {
	type: "json",
};
import supersededFixture from "../../fixtures/recall.memory.superseded.json" with {
	type: "json",
};
import type { Snapshot } from "../live.js";
import { DEFAULT_ROUTE, type Route } from "../route.js";
import type {
	Memory,
	MemoryContradiction,
	MemoryNeighbors,
	RecallResult,
	StaleRefsReport,
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
	staleBySlug,
	TOPIC_ROW_CAP,
} from "./memory.js";

const memory = unwrap<Memory>("memory_get", memoryGetFixture);
const listed = unwrap<Memory[]>("memory_list", memoryListFixture);
const neighbors = unwrap<RecallResult>("recall", neighborsFixture)
	.memory as MemoryNeighbors;
const superseded = unwrap<RecallResult>("recall", supersededFixture)
	.memory as MemoryNeighbors;
const pairs = unwrap<MemoryContradiction[]>(
	"memory_contradictions",
	contradictionsFixture,
);
const recalled = unwrap<RecallResult>("recall", recallFixture);
const report = unwrap<StaleRefsReport>("memory_stale_refs", staleRefsFixture);

const route: Route = { ...DEFAULT_ROUTE, view: "memory" };

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement("div");
	document.body.replaceChildren(root);
});

function text(): string {
	return root.textContent ?? "";
}

function draw(
	page: MemoryPage,
	at: Route = route,
	onNavigate = vi.fn(),
	stale: Snapshot<StaleRefsReport> | null = null,
) {
	render(memoryView(page, at, onNavigate, stale), root);
	return onNavigate;
}

function landed(data: StaleRefsReport): Snapshot<StaleRefsReport> {
	return { data, error: null, loadedAt: 1, loading: false };
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
			{ mode: "topic", topic: null, memories: [], readAtLimit: false },
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
			{ mode: "topic", topic: null, memories: [], readAtLimit: false },
			{ ...route, topic: "tp-gone" },
		);

		expect(text()).toContain("No topic tp-gone");
		expect(root.querySelector("table")).toBeNull();
	});

	it("offers no control the topic read cannot honour, and no column it does not send", () => {
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
				readAtLimit: false,
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

describe("a topic at its limits", () => {
	const topic = {
		slug: "tp-topic-mcp",
		name: "mcp",
		kind: "topic",
		created_at: memory.created_at,
	};
	const at = { ...route, topic: topic.slug };

	it("warns a filtered empty topic that its read was cut", () => {
		draw({ mode: "topic", topic, memories: [], readAtLimit: true }, at);

		expect(root.querySelector(".note")?.textContent).toContain(
			"came back at its limit",
		);
		expect(text()).toContain("No memories are tagged with this topic.");
	});

	it("caps the rows after narrowing, and says how many it left out", () => {
		const many = Array.from({ length: TOPIC_ROW_CAP + 3 }, (_, i) => ({
			...memory,
			slug: `pat-${i}`,
		}));
		draw({ mode: "topic", topic, memories: many, readAtLimit: false }, at);

		expect(root.querySelectorAll("tbody tr")).toHaveLength(TOPIC_ROW_CAP);
		expect(root.querySelector(".note")?.textContent).toContain(
			`first ${TOPIC_ROW_CAP} of ${TOPIC_ROW_CAP + 3}`,
		);
	});

	it("says nothing about limits for a topic under them", () => {
		draw({ mode: "topic", topic, memories: [memory], readAtLimit: false }, at);

		expect(root.querySelector(".note")).toBeNull();
	});
});

describe("stale symbol refs", () => {
	const browse: MemoryPage = { mode: "browse", memories: listed, inbox: [] };
	const byTitle = (title: string) => {
		const found = listed.find((m) => m.title === title);
		if (!found) {
			throw new Error(`no memory titled ${title} in the fixture`);
		}
		return found;
	};
	// The seed: one memory citing a moved symbol, one citing a repo nobody
	// indexed. Both are in the report; only the first is stale.
	const moved = byTitle("Service.run retries twice before it raises");
	const unchecked = byTitle("The index view renders without a login");

	function rowOf(slug: string): Element | null | undefined {
		return root.querySelector(`tbody a[href$="slug=${slug}"]`)?.closest("tr");
	}

	it("marks a memory with a missing ref, and not one with only unknown refs", () => {
		draw(browse, route, vi.fn(), landed(report));

		expect(
			rowOf(moved.slug)?.querySelector(".badge.stale-refs"),
		).not.toBeNull();
		expect(rowOf(unchecked.slug)).not.toBeNull();
		expect(rowOf(unchecked.slug)?.querySelector(".badge")?.textContent).toBe(
			"pattern",
		);
		expect(
			rowOf(unchecked.slug)?.querySelector(".badge.stale-refs"),
		).toBeNull();
		expect(root.querySelectorAll("tbody .badge.stale-refs")).toHaveLength(1);
	});

	it("names the missing ref and where the symbol may have gone", () => {
		draw(browse, route, vi.fn(), landed(report));

		const title =
			rowOf(moved.slug)
				?.querySelector(".badge.stale-refs")
				?.getAttribute("title") ?? "";
		expect(title).toContain("lib/old_service.py::Service.run");
		expect(title).toContain("same name at");
		expect(title).toContain("lib/service.py::Service.run");
	});

	it("counts the unknown refs apart from the stale memories", () => {
		draw(browse, route, vi.fn(), landed(report));

		const note = root.querySelector(".stale-summary")?.textContent ?? "";
		expect(note).toMatch(/1 memory in any repo\s+cites a symbol/);
		expect(note).toMatch(/1 ref could not be\s+checked/);
	});

	it("narrows to stale memories only when asked", () => {
		draw(browse, { ...route, stale: true }, vi.fn(), landed(report));

		const rows = root.querySelectorAll("tbody tr");
		expect(rows).toHaveLength(1);
		expect(rows[0]?.textContent).toContain(moved.title);
	});

	it("toggles the filter through the route", () => {
		const onNavigate = draw(browse, route, vi.fn(), landed(report));

		const box = [...root.querySelectorAll("label")]
			.find((label) => label.textContent?.includes("Stale refs only"))
			?.querySelector("input");
		box?.click();

		expect(onNavigate).toHaveBeenCalledWith({ stale: true });
	});

	it("says the check is pending rather than showing an empty filter as clean", () => {
		draw(browse, { ...route, stale: true }, vi.fn(), {
			data: null,
			error: null,
			loadedAt: null,
			loading: true,
		});

		expect(root.querySelector(".stale-summary")?.textContent).toContain(
			"Checking symbol refs",
		);
		expect(root.querySelector("tbody")).toBeNull();
	});

	it("says no memory is marked when the check failed", () => {
		draw(browse, route, vi.fn(), {
			data: null,
			error: new Error("code graph unreachable"),
			loadedAt: null,
			loading: false,
		});

		expect(root.querySelector(".stale-summary")?.textContent).toMatch(
			/no memory is\s+marked stale/,
		);
		expect(root.querySelectorAll("tbody tr")).toHaveLength(listed.length);
	});

	it("draws no note when every checked ref resolves", () => {
		draw(
			browse,
			route,
			vi.fn(),
			landed({
				repo: null,
				checked: 1,
				counts: { live: 1, missing: 0, unknown: 0 },
				memories: [],
			}),
		);

		expect(root.querySelector(".stale-summary")).toBeNull();
	});

	it("marks each ref in the panel: missing with its candidate, unknown with its reason", () => {
		const flagged = staleBySlug(report);
		// `memory_list` rows carry no refs; the panel's `memory_get` does.
		const withRefs = (m: Memory): Memory => ({
			...m,
			symbol_refs: flagged.get(m.slug)?.refs.map((ref) => ref.symbol_ref),
		});
		render(
			memoryDetail(
				{ memory: withRefs(moved), neighbors },
				route,
				flagged.get(moved.slug),
			),
			root,
		);
		expect(root.querySelector(".crumbs .badge.stale-refs")).not.toBeNull();
		const missing = root.querySelector("li.ref-missing");
		expect(missing?.textContent).toContain("lib/old_service.py::Service.run");
		expect(missing?.textContent).toContain("Same name in the code graph");
		// The live ref is the bare id.
		expect(root.querySelectorAll("ul.symbols li")).toHaveLength(2);

		render(
			memoryDetail(
				{ memory: withRefs(unchecked), neighbors },
				route,
				flagged.get(unchecked.slug),
			),
			root,
		);
		expect(root.querySelector(".crumbs .badge.stale-refs")).toBeNull();
		const unknown = root.querySelector("li.ref-unknown");
		expect(unknown?.querySelector(".badge.unchecked")).not.toBeNull();
		expect(unknown?.textContent).toContain("is not indexed");
		expect(root.querySelector("li.ref-missing")).toBeNull();
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
			symbols: [],
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
