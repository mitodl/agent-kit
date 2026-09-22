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
			view: "projects",
			repo: "https://github.com/mitodl/agent-kit",
			project: "wp-x",
			slug: "tk-y",
			closed: true,
		});
	});

	it("keeps the parts it understands when the view is junk", () => {
		// Field-by-field fallback, not wholesale: a link written against a later
		// version should still open the task it names.
		const route = parseRoute("#nonsense?slug=tk-y");
		expect(route.view).toBe(DEFAULT_ROUTE.view);
		expect(route.slug).toBe("tk-y");
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
		};
		expect(parseRoute(formatRoute(route))).toEqual(route);
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
