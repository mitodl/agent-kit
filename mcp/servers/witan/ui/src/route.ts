import { canonicalRepo } from "./format.js";
import { VIEWS, type ViewId } from "./shell.js";
import type { MemoryKind } from "./types.js";

const MEMORY_KINDS: readonly MemoryKind[] = [
	"pattern",
	"project_fact",
	"lesson",
	"agent_context",
];

/**
 * The memory view's narrowing that happens in the browser, over whatever the
 * read returned. Each is one exact value or `null` for "any".
 *
 * Not tool arguments: only `memory_list` takes `language`, and none of the
 * reads takes the rest, so filtering here is the one way they mean the same
 * thing in list, search and recall mode.
 */
export const MEMORY_FACETS = [
	"language",
	"category",
	"severity",
	"tag",
	"author",
] as const;

export type MemoryFacet = (typeof MEMORY_FACETS)[number];

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
	/** The memory view's search text. Empty is the browse state. */
	q: string;
	/** One memory kind, else `null` for all four. A tool argument. */
	kind: MemoryKind | null;
	/** Plain BM25 (`memory_search`) rather than `recall`. */
	plain: boolean;
	/** Whether superseded memories are shown. Off, as the tools default. */
	superseded: boolean;
	/** A `tp-` slug when the memory view is showing one topic, else `null`. */
	topic: string | null;
	facets: Record<MemoryFacet, string | null>;
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
	q: "",
	kind: null,
	plain: false,
	superseded: false,
	topic: null,
	facets: {
		language: null,
		category: null,
		severity: null,
		tag: null,
		author: null,
	},
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
	const kind = MEMORY_KINDS.find(
		(candidate) => candidate === params.get("kind"),
	);
	return {
		view: view ? view.id : DEFAULT_ROUTE.view,
		// Canonical from here on, so every tool call and every browser-side
		// comparison sees the key the server joins on (see `canonicalRepo`).
		repo: canonicalRepo(params.get("repo") ?? DEFAULT_ROUTE.repo),
		project: params.get("project") || null,
		slug: params.get("slug") || null,
		closed: params.get("closed") === "1",
		days: parseDays(params.get("days")),
		q: params.get("q") ?? DEFAULT_ROUTE.q,
		kind: kind ?? null,
		plain: params.get("plain") === "1",
		superseded: params.get("superseded") === "1",
		topic: params.get("topic") || null,
		facets: Object.fromEntries(
			MEMORY_FACETS.map((facet) => [facet, params.get(facet) || null]),
		) as Route["facets"],
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
	if (route.q) {
		params.set("q", route.q);
	}
	if (route.kind) {
		params.set("kind", route.kind);
	}
	if (route.plain) {
		params.set("plain", "1");
	}
	if (route.superseded) {
		params.set("superseded", "1");
	}
	if (route.topic) {
		params.set("topic", route.topic);
	}
	for (const facet of MEMORY_FACETS) {
		const value = route.facets[facet];
		if (value) {
			params.set(facet, value);
		}
	}
	const query = params.toString();
	return query ? `#${route.view}?${query}` : `#${route.view}`;
}

/** A fragment for `route` with `patch` applied. What every link in the app uses. */
export function routeHref(route: Route, patch: Partial<Route>): string {
	return formatRoute({ ...route, ...patch });
}
