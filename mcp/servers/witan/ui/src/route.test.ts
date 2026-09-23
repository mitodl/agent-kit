import { describe, expect, it } from "vitest";
import {
	DEFAULT_ROUTE,
	formatRoute,
	parseRoute,
	type Route,
	routeHref,
} from "./route.js";

describe("parseRoute", () => {
	it("reads a bare view fragment", () => {
		expect(parseRoute("#board")).toEqual({ ...DEFAULT_ROUTE, view: "board" });
	});

	it("tolerates a fragment with no leading hash", () => {
		expect(parseRoute("board").view).toBe("board");
	});

	it("falls back to the first view for an unknown one", () => {
		// A stale bookmark has to land somewhere: there is no 404 to serve from a
		// URL fragment, and an unmatched id would otherwise render an empty frame.
		expect(parseRoute("#no-such-view").view).toBe(DEFAULT_ROUTE.view);
	});

	it("falls back for an empty fragment", () => {
		expect(parseRoute("")).toEqual(DEFAULT_ROUTE);
	});

	it("reads the filters and the open slug", () => {
		expect(
			parseRoute(
				"#projects?repo=https%3A%2F%2Fgithub.com%2Fmitodl%2Fagent-kit&project=wp-x&slug=tk-y&closed=1",
			),
		).toEqual({
			...DEFAULT_ROUTE,
			view: "projects",
			repo: "https://github.com/mitodl/agent-kit",
			project: "wp-x",
			slug: "tk-y",
			closed: true,
			days: 14,
		});
	});

	it("reads the memory view's search, toggles and facets", () => {
		const route = parseRoute(
			"#memory?q=wrap+flag&kind=lesson&plain=1&superseded=1&topic=tp-topic-mcp&tag=mcp&author=fixtures",
		);
		expect(route.q).toBe("wrap flag");
		expect(route.kind).toBe("lesson");
		expect(route.plain).toBe(true);
		expect(route.superseded).toBe(true);
		expect(route.topic).toBe("tp-topic-mcp");
		expect(route.facets).toEqual({
			...DEFAULT_ROUTE.facets,
			tag: "mcp",
			author: "fixtures",
		});
	});

	it("drops a memory kind the server does not have", () => {
		// It is passed straight to the tools, which would refuse the call.
		expect(parseRoute("#memory?kind=rumour").kind).toBeNull();
	});

	it("keeps the parts it understands when the view is junk", () => {
		// Field-by-field fallback, not wholesale: a link written against a later
		// version should still open the task it names.
		const route = parseRoute("#nonsense?slug=tk-y");
		expect(route.view).toBe(DEFAULT_ROUTE.view);
		expect(route.slug).toBe("tk-y");
	});

	it("canonicalizes the repo, as the tools will", () => {
		// Otherwise Ready (scoped by the server, which canonicalizes) and the
		// columns narrowed in the browser would answer different questions.
		const route = parseRoute(
			"#board?repo=git%40github.com%3AMITODL%2Fagent-kit.git",
		);

		expect(route.repo).toBe("https://github.com/mitodl/agent-kit");
	});

	it("treats an empty filter value as absent", () => {
		expect(parseRoute("#projects?project=&slug=").project).toBeNull();
		expect(parseRoute("#projects?project=&slug=").slug).toBeNull();
	});
});

describe("formatRoute", () => {
	it("emits only the non-default fields", () => {
		expect(formatRoute(DEFAULT_ROUTE)).toBe("#projects");
		expect(formatRoute({ ...DEFAULT_ROUTE, view: "board" })).toBe("#board");
	});

	it("round-trips every field", () => {
		const route: Route = {
			view: "timeline",
			repo: "https://github.com/mitodl/agent-kit",
			project: "wp-x",
			slug: "tk-y",
			closed: true,
			days: 30,
			q: "wrap flag",
			kind: "pattern",
			plain: true,
			superseded: true,
			topic: "tp-topic-mcp",
			facets: {
				language: "python",
				category: "testing",
				severity: "warning",
				tag: "mcp",
				author: "fixtures",
			},
			contract: "endpoint",
			confidence: 0.7,
			hideGeneric: true,
			precise: true,
			edge: "https://github.com/example/web|https://github.com/example/api",
			// A key with its own colons: the value splits on the first one only.
			binding: "service:repo:https://github.com/example/web",
		};
		expect(parseRoute(formatRoute(route))).toEqual(route);
	});

	it("falls back to the server's floor for a confidence it does not offer", () => {
		// Below 0.5 would promise edges the server already dropped.
		expect(parseRoute("#bridge?confidence=0.1").confidence).toBe(0.5);
		expect(parseRoute("#bridge?confidence=0.9").confidence).toBe(0.9);
	});

	it("drops a contract kind it does not know", () => {
		expect(parseRoute("#bridge?contract=database").contract).toBeNull();
	});

	it("falls back to the default window for one it does not offer", () => {
		expect(parseRoute("#timeline?days=100000").days).toBe(DEFAULT_ROUTE.days);
		expect(parseRoute("#timeline?days=abc").days).toBe(DEFAULT_ROUTE.days);
		expect(parseRoute("#timeline?days=7").days).toBe(7);
	});

	it("round-trips a repo URI that needs escaping", () => {
		const route = { ...DEFAULT_ROUTE, repo: "https://github.com/a/b c" };
		expect(parseRoute(formatRoute(route)).repo).toBe(route.repo);
	});
});

describe("routeHref", () => {
	it("applies the patch over the current route", () => {
		const route: Route = { ...DEFAULT_ROUTE, project: "wp-x" };
		expect(routeHref(route, { slug: "tk-y" })).toBe(
			"#projects?project=wp-x&slug=tk-y",
		);
	});

	it("clears a field when the patch nulls it", () => {
		const route: Route = { ...DEFAULT_ROUTE, project: "wp-x", slug: "tk-y" };
		expect(routeHref(route, { slug: null })).toBe("#projects?project=wp-x");
	});
});
