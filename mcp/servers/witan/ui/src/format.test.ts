import { describe, expect, it } from "vitest";
import {
	absolute,
	ago,
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
