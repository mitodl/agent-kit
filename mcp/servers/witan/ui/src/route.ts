import { canonicalRepo } from "./format.js";
import { VIEWS, type ViewId } from "./shell.js";

/**
 * The app's whole UI state, as the URL carries it.
 *
 * Spec §6.1: "The URL carries view, filters and the open slug, so a link to a
 * stale claim can be pasted to whoever holds it." That is why every piece of
 * state a person can change lives here rather than in a module variable — a
 * filtered board or an open task has to survive being copied out of the
 * address bar.
 *
 * ★ IT STAYS IN THE FRAGMENT, not the path. `src/mcp.ts` resolves `/mcp` by
 * cutting at the `/ui/` mount, so a path route would work too; the fragment is
 * chosen because it never reaches the server, so no route here can 404 against
 * a deployment whose catch-all is configured differently from `witan ui`.
 */
export interface Route {
	view: ViewId;
	/** The `repo` argument passed to every tool. `""` means all repos. */
	repo: string;
	/** A `wp-` slug when one project is drilled into, else `null`. */
	project: string | null;
	/** The slug open in the detail panel, else `null`. */
	slug: string | null;
	/** Whether closed tasks are listed. Off by default, as the CLI has it. */
	closed: boolean;
	/** The timeline's window, in days back from the read. One of `WINDOW_DAYS`. */
	days: number;
}

/**
 * The windows the timeline offers. A closed set rather than free input, so a
 * hand-edited `days=100000` cannot ask for every session ever recorded, which
 * is the unbounded read `since` exists to avoid (spec §3.7).
 */
export const WINDOW_DAYS = [7, 14, 30, 90] as const;

/**
 * The route a page with no fragment starts at.
 *
 * `repo: ""` — all repos — rather than a detected one. The tools fall back to
 * the SERVER's working directory when `repo` is omitted, and a deployed witan
 * has no checkout, so "the current repo" is not something this page can
 * resolve. Asking for everything and filtering down is the one behaviour that
 * means the same thing in both modes.
 */
export const DEFAULT_ROUTE: Route = {
	view: VIEWS[0].id,
	repo: "",
	project: null,
	slug: null,
	closed: false,
	// Spec §6.6: "where the last two weeks went".
	days: 14,
};

/**
 * Read a route out of a location fragment.
 *
 * Shaped `#view?key=value`: the view is a path-like head so an old bookmark of
 * a bare `#board` still resolves, and the filters are a query string so adding
 * one later is not a positional migration.
 *
 * Unparseable input falls back field by field rather than wholesale. Someone
 * hand-editing the URL, or following a link written against a later version,
 * should land on the parts that still make sense instead of being reset to the
 * front page.
 */
export function parseRoute(hash: string): Route {
	const [head = "", query = ""] = hash.replace(/^#/, "").split("?", 2);
	const params = new URLSearchParams(query);
	const view = VIEWS.find((candidate) => candidate.id === head);
	return {
		view: view ? view.id : DEFAULT_ROUTE.view,
		// Canonical from here on, so every tool call and every browser-side
		// comparison sees the key the server joins on (see `canonicalRepo`).
		repo: canonicalRepo(params.get("repo") ?? DEFAULT_ROUTE.repo),
		project: params.get("project") || null,
		slug: params.get("slug") || null,
		closed: params.get("closed") === "1",
		days: parseDays(params.get("days")),
	};
}

function parseDays(value: string | null): number {
	const days = Number(value);
	return (WINDOW_DAYS as readonly number[]).includes(days)
		? days
		: DEFAULT_ROUTE.days;
}

/**
 * Write a route back out as a fragment.
 *
 * Only non-default fields are emitted, so the common case is a short URL and
 * two routes that mean the same thing compare equal as strings — which is what
 * lets the app skip a re-render on a no-op navigation. `formatRoute` and
 * `parseRoute` round-trip; `route.test.ts` holds them to it.
 */
export function formatRoute(route: Route): string {
	const params = new URLSearchParams();
	if (route.repo !== DEFAULT_ROUTE.repo) {
		params.set("repo", route.repo);
	}
	if (route.project) {
		params.set("project", route.project);
	}
	if (route.slug) {
		params.set("slug", route.slug);
	}
	if (route.closed) {
		params.set("closed", "1");
	}
	if (route.days !== DEFAULT_ROUTE.days) {
		params.set("days", String(route.days));
	}
	const query = params.toString();
	return query ? `#${route.view}?${query}` : `#${route.view}`;
}

/** A fragment for `route` with `patch` applied. What every link in the app uses. */
export function routeHref(route: Route, patch: Partial<Route>): string {
	return formatRoute({ ...route, ...patch });
}
