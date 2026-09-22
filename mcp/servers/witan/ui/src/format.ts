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

/**
 * Hosts whose org/repo path is case-insensitive, so folding it cannot merge
 * two distinct repos. `witan_core.repo_key._CASE_INSENSITIVE_PATH_HOSTS`.
 */
const CASE_INSENSITIVE_PATH_HOSTS = new Set(["github.com", "gitlab.com"]);

/**
 * A git remote as the canonical repo key the server joins on.
 *
 * ★ A COPY OF `witan_core.repo_key.normalise`, AND PINNED TO IT. The tools
 * canonicalize their `repo` argument, so a board scoped to
 * `git@github.com:Org/Repo.git` reads a correctly scoped Ready column from the
 * server while every other column, narrowed here, compares the raw string and
 * comes out empty. Canonicalizing the route once makes both sides answer the
 * same question. `fixtures/repo-keys.json` is recorded from the Python
 * function by `just ui-fixtures` and `format.test.ts` holds this to it, so a
 * change to the rule there fails CI until this follows.
 *
 * Both patterns are anchored because Python's `re.match` is, and the SSH one
 * is tried first because Python tries it first: an HTTPS URL with userinfo
 * (`https://token@host/org/repo`) matches it, which is harmless only because
 * the result is the same.
 */
export function canonicalRepo(url: string): string {
	const stripped = url
		.trim()
		.replace(/\/+$/, "")
		.replace(/\.git$/, "");
	const match =
		/^(?:ssh:\/\/)?[^@]+@([^:/]+)[:/](.+)/.exec(stripped) ??
		/^https?:\/\/(?:[^@/]+@)?([^/]+)\/(.+)/.exec(stripped);
	if (!match) {
		return stripped;
	}
	const host = (match[1] ?? "").toLowerCase();
	const path = match[2] ?? "";
	return `https://${host}/${
		CASE_INSENSITIVE_PATH_HOSTS.has(host) ? path.toLowerCase() : path
	}`;
}

/**
 * The path a forge puts between a repo URL and a branch name.
 *
 * ★ A TABLE, NOT A DEFAULT. `/tree/` is GitHub's shape, and witan canonicalizes
 * every remote it sees, including GitLab ones, where the branch lives under
 * `/-/tree/` instead. Assuming GitHub for anything with an https: scheme
 * generated confidently broken links for every other host — and a self-hosted
 * instance of either forge is indistinguishable by hostname, so it is not a
 * guess worth making. An unrecognized host gets no link at all.
 */
const FORGE_TREE_PATHS: Record<string, string> = {
	"github.com": "tree",
	"gitlab.com": "-/tree",
};

/**
 * A URL for a branch on its forge, or `null` when the host is not one we know.
 *
 * `null` is the common, correct answer for a self-hosted or unfamiliar remote:
 * the caller renders the branch name as text, which is no worse than today and
 * strictly better than a link that 404s.
 */
export function branchUrl(
	repo: string | null | undefined,
	branch: string,
): string | null {
	if (!isLinkable(repo) || !branch) {
		return null;
	}
	const base = new URL(repo);
	const treePath = FORGE_TREE_PATHS[base.hostname.toLowerCase()];
	if (!treePath) {
		return null;
	}
	const path = base.pathname.replace(/\.git$/, "").replace(/\/$/, "");
	// Each segment escaped, the separators kept: a branch name may contain
	// slashes (`renovate/astral-sh-ruff`) and they are path separators on the
	// forge, but anything else in a segment is not.
	const ref = branch.split("/").map(encodeURIComponent).join("/");
	return `${base.origin}${path}/${treePath}/${ref}`;
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
