import { describe, expect, it } from "vitest";
import { mcpEndpoint } from "./mcp.js";

/**
 * ★ THE ONE THING IN THIS LAYER THAT HAS BROKEN TWICE, BOTH TIMES SILENTLY.
 *
 * Nothing imports `mcp.ts` yet, so neither a build nor a view exercises the
 * endpoint; `tsc` cannot check a URL string. Both regressions therefore
 * reached `main`: `"mcp"` resolved to /ui/mcp from any document, and
 * `"../mcp"` resolved correctly only from a document exactly one segment
 * under the mount.
 *
 * The path-routed cases below are the ones that were broken and could not
 * have been caught: they are what spec §6.1's "the URL carries view, filters
 * and the open slug" produces the moment a view uses `history.pushState`
 * rather than a fragment.
 */
describe("mcpEndpoint", () => {
	it.each([
		// [document URL, expected endpoint]
		["http://127.0.0.1:8765/ui/", "http://127.0.0.1:8765/mcp"],
		["http://127.0.0.1:8765/ui/index.html", "http://127.0.0.1:8765/mcp"],
		["https://witan.example.org/ui/", "https://witan.example.org/mcp"],
		// A reverse-proxy prefix: the endpoint has to move with it, which is
		// why this resolves against the document rather than the origin.
		["https://h/witan/ui/", "https://h/witan/mcp"],
		// Path-routed views. "../mcp" got every one of these wrong.
		["http://127.0.0.1:8765/ui/board/tk-x", "http://127.0.0.1:8765/mcp"],
		["http://127.0.0.1:8765/ui/board/tk-x/", "http://127.0.0.1:8765/mcp"],
		["https://h/witan/ui/board/tk-x", "https://h/witan/mcp"],
		["https://h/witan/ui/memory/pat-a/neighbours", "https://h/witan/mcp"],
	])("resolves %s to %s", (documentUrl, expected) => {
		expect(mcpEndpoint(documentUrl, "/mcp").href).toBe(expected);
	});

	it("keeps the query and fragment out of the endpoint", () => {
		const url = mcpEndpoint("http://127.0.0.1:8765/ui/?repo=x#board", "/mcp");

		expect(url.href).toBe("http://127.0.0.1:8765/mcp");
	});

	it("falls back to the root when the document is not under the mount", () => {
		// A dev server or a future remount. Answering /mcp is the useful
		// guess; the alternative is throwing on a page that might still work.
		expect(mcpEndpoint("http://localhost:5173/", "/mcp").href).toBe(
			"http://localhost:5173/mcp",
		);
	});

	it.each([
		// `witan serve --path` moves the protocol endpoint; `/ui/` does not move
		// with it, so a page that assumed /mcp would read nothing.
		["http://127.0.0.1:8765/ui/", "/api/mcp", "http://127.0.0.1:8765/api/mcp"],
		["https://h/witan/ui/board/x", "/api/mcp", "https://h/witan/api/mcp"],
		// The CLI normalizes a missing leading slash before advertising it, but
		// the join must not double up if one arrives anyway.
		["http://127.0.0.1:8765/ui/", "mcp", "http://127.0.0.1:8765/mcp"],
	])("honours a configured path: %s + %s", (documentUrl, path, expected) => {
		expect(mcpEndpoint(documentUrl, path).href).toBe(expected);
	});

	it("anchors on the LAST mount segment", () => {
		// A prefix that itself contains the mount name. Taking the first
		// occurrence would point at the proxy's own root.
		expect(mcpEndpoint("https://h/ui/witan/ui/board/x", "/mcp").href).toBe(
			"https://h/ui/witan/mcp",
		);
	});
});
