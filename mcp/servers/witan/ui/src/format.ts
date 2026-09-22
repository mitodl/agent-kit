/**
 * Turning what the tools return into what a person reads.
 *
 * Kept out of the views so the board, the timeline and the widgets all render
 * a claim age or a repo the same way, and so the timestamp rule below is
 * written down once.
 */

/**
 * Parse a timestamp the tools returned.
 *
 * ★ A NAIVE STRING IS UTC, AND JAVASCRIPT DISAGREES. The graph stores most
 * timestamps zone-less (`task_get` returns `2026-09-22T14:46:32.427`) while a
 * few carry an offset (`task_claim` returns the same instant as
 * `…+00:00`). `new Date()` reads the ISO date-time form without an offset as
 * LOCAL time, so a browser away from UTC renders every stored timestamp
 * shifted by its offset, and a claim lease age with it: hours too old east of
 * UTC, hours too young west of it, and silently either way.
 *
 * Returns `null` rather than an `Invalid Date` so callers can only either
 * render a date or not render one.
 */
export function parseTimestamp(value: string | null | undefined): Date | null {
	if (!value) {
		return null;
	}
	const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
	const parsed = new Date(zoned);
	return Number.isNaN(parsed.getTime()) ? null : parsed;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * How long ago, coarsely.
 *
 * Coarse on purpose: the views this feeds — a claim lease age, a last-read
 * time, a task's timestamps — are all read to answer "is this current", and a
 * second-resolution answer invites a precision the 30s poll does not have.
 */
export function ago(
	value: string | Date | null | undefined,
	now = Date.now(),
): string {
	const at = value instanceof Date ? value : parseTimestamp(value);
	if (!at) {
		return "—";
	}
	const elapsed = now - at.getTime();
	if (elapsed < 0) {
		// Clock skew between the browser and the server, not a future event.
		return "just now";
	}
	if (elapsed < MINUTE) {
		return "just now";
	}
	if (elapsed < HOUR) {
		return `${Math.floor(elapsed / MINUTE)}m ago`;
	}
	if (elapsed < DAY) {
		return `${Math.floor(elapsed / HOUR)}h ago`;
	}
	return `${Math.floor(elapsed / DAY)}d ago`;
}

/** The full instant, for a `title=` beside every `ago`. */
export function absolute(value: string | Date | null | undefined): string {
	const at = value instanceof Date ? value : parseTimestamp(value);
	return at
		? at
				.toISOString()
				.replace("T", " ")
				.replace(/\.\d+Z$/, "Z")
		: "—";
}

/**
 * A repo URI as `owner/name`.
 *
 * The tools carry repos as canonical URIs and the filter chrome has to fit
 * several across a top bar. The full URI stays in the `title` and in the URL,
 * so nothing is lost by shortening the label.
 */
export function repoLabel(repo: string | null | undefined): string {
	if (!repo) {
		return "all repos";
	}
	const path = repo.replace(/^https?:\/\/[^/]+\//, "").replace(/\.git$/, "");
	return path || repo;
}

/** Whether a value can be used as an `href`. `external_uri` is free text. */
export function isLinkable(uri: string | null | undefined): uri is string {
	if (!uri) {
		return false;
	}
	try {
		const parsed = new URL(uri);
		return parsed.protocol === "https:" || parsed.protocol === "http:";
	} catch {
		return false;
	}
}
