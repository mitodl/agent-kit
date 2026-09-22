import { describe, expect, it } from "vitest";
import repoKeys from "../fixtures/repo-keys.json" with { type: "json" };
import {
	absolute,
	ago,
	branchUrl,
	canonicalRepo,
	isLinkable,
	parseTimestamp,
	repoLabel,
} from "./format.js";

describe("parseTimestamp", () => {
	it("reads a zone-less timestamp as UTC", () => {
		// ★ THE BUG THIS EXISTS TO STOP. The graph stores most timestamps
		// zone-less, and `new Date()` reads that form as LOCAL time, so every
		// stored instant would render shifted by the viewer's offset — and a
		// claim lease age with it.
		expect(parseTimestamp("2026-09-22T14:46:32.427")?.toISOString()).toBe(
			"2026-09-22T14:46:32.427Z",
		);
	});

	it("reads an offset timestamp as the instant it names", () => {
		expect(
			parseTimestamp("2026-09-22T14:46:31.955936+00:00")?.toISOString(),
		).toBe("2026-09-22T14:46:31.955Z");
	});

	it("agrees between the two spellings of one instant", () => {
		// `task_get` and `task_claim` return the same claim this way.
		const naive = parseTimestamp("2026-09-22T14:46:31.955");
		const zoned = parseTimestamp("2026-09-22T14:46:31.955+00:00");
		expect(naive?.getTime()).toBe(zoned?.getTime());
	});

	it("reads a non-UTC offset", () => {
		expect(parseTimestamp("2026-09-22T10:00:00-04:00")?.toISOString()).toBe(
			"2026-09-22T14:00:00.000Z",
		);
	});

	it("returns null rather than an invalid date", () => {
		expect(parseTimestamp("not a date")).toBeNull();
		expect(parseTimestamp(null)).toBeNull();
		expect(parseTimestamp("")).toBeNull();
	});
});

describe("ago", () => {
	const now = Date.parse("2026-09-22T12:00:00Z");

	it("scales through the units", () => {
		expect(ago("2026-09-22T11:59:30", now)).toBe("just now");
		expect(ago("2026-09-22T11:45:00", now)).toBe("15m ago");
		expect(ago("2026-09-22T09:00:00", now)).toBe("3h ago");
		expect(ago("2026-09-20T12:00:00", now)).toBe("2d ago");
	});

	it("does not render clock skew as the future", () => {
		expect(ago("2026-09-22T12:00:30", now)).toBe("just now");
	});

	it("renders an absent timestamp as a dash", () => {
		expect(ago(null, now)).toBe("—");
	});
});

describe("absolute", () => {
	it("drops the sub-second noise", () => {
		expect(absolute("2026-09-22T14:46:32.427")).toBe("2026-09-22 14:46:32Z");
	});
});

describe("repoLabel", () => {
	it("shortens a canonical repo URI", () => {
		expect(repoLabel("https://github.com/mitodl/agent-kit")).toBe(
			"mitodl/agent-kit",
		);
	});

	it("drops a .git suffix", () => {
		expect(repoLabel("https://github.com/mitodl/agent-kit.git")).toBe(
			"mitodl/agent-kit",
		);
	});

	it("names the all-repos scope", () => {
		expect(repoLabel("")).toBe("all repos");
		expect(repoLabel(null)).toBe("all repos");
	});
});

describe("branchUrl", () => {
	it("builds a GitHub branch URL", () => {
		expect(
			branchUrl("https://github.com/mitodl/agent-kit", "witan-ui-drill-down"),
		).toBe("https://github.com/mitodl/agent-kit/tree/witan-ui-drill-down");
	});

	it("uses GitLab's own branch path", () => {
		// ★ NOT `/tree/`. witan canonicalizes GitLab remotes too, and assuming
		// GitHub's shape for them produced a confidently broken link.
		expect(branchUrl("https://gitlab.com/group/proj", "main")).toBe(
			"https://gitlab.com/group/proj/-/tree/main",
		);
	});

	it("declines a host it does not recognize", () => {
		// Including self-hosted GitHub and GitLab, which are indistinguishable
		// by hostname. The caller renders the branch name as text.
		expect(branchUrl("https://git.example.org/team/proj", "main")).toBeNull();
		expect(branchUrl("some-local-thing", "main")).toBeNull();
		expect(branchUrl(null, "main")).toBeNull();
	});

	it("keeps slashes in a branch name as separators", () => {
		expect(
			branchUrl(
				"https://github.com/mitodl/agent-kit",
				"renovate/astral-sh-ruff",
			),
		).toBe("https://github.com/mitodl/agent-kit/tree/renovate/astral-sh-ruff");
	});

	it("escapes what is not a separator", () => {
		expect(branchUrl("https://github.com/o/r", "fix/a b#c")).toBe(
			"https://github.com/o/r/tree/fix/a%20b%23c",
		);
	});

	it("drops a .git suffix and a trailing slash", () => {
		expect(branchUrl("https://github.com/o/r.git", "main")).toBe(
			"https://github.com/o/r/tree/main",
		);
		expect(branchUrl("https://github.com/o/r/", "main")).toBe(
			"https://github.com/o/r/tree/main",
		);
	});
});

describe("isLinkable", () => {
	it("accepts http and https", () => {
		expect(isLinkable("https://github.com/mitodl/agent-kit/issues/1")).toBe(
			true,
		);
		expect(isLinkable("http://example.test/x")).toBe(true);
	});

	it("rejects anything else external_uri might hold", () => {
		// `external_uri` is free text, so it can be a bare id, a note, or a
		// scheme a browser should not follow from a page.
		expect(isLinkable("mitodl/hq#12175")).toBe(false);
		expect(isLinkable("javascript:alert(1)")).toBe(false);
		expect(isLinkable(null)).toBe(false);
	});
});

describe("canonicalRepo", () => {
	// ★ Recorded from `witan_core.repo_key.normalise` itself by
	// `just ui-fixtures`, so a change to the server's rule fails here (and
	// `ui-fixtures-check` fails in CI) until this copy follows.
	it.each(
		Object.entries(repoKeys as Record<string, string>),
	)("canonicalizes %j as the server does", (url, expected) => {
		expect(canonicalRepo(url)).toBe(expected);
	});

	it("leaves the all-repos value alone", () => {
		expect(canonicalRepo("")).toBe("");
	});
});
