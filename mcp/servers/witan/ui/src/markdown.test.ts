import { render } from "lit-html";
import markdownIt, { type Token } from "markdown-it";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { markdown } from "./markdown.js";

// From a code point: the formatter turns a `\u` string escape into the
// literal character, which then reverses this file in editors and diffs.
const RLO = String.fromCodePoint(0x202e);

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement("div");
	document.body.replaceChildren(root);
});

afterEach(() => {
	vi.restoreAllMocks();
});

function draw(source: string): HTMLElement {
	render(markdown(source), root);
	return root;
}

function text(): string {
	return root.textContent ?? "";
}

function tags(): Set<string> {
	return new Set([...root.querySelectorAll("*")].map((e) => e.tagName));
}

/** Rewrite the real parse's tokens, for a token type no input produces. */
function withTokens(rewrite: (tokens: Token[]) => void): void {
	const parse = markdownIt.prototype.parse;
	vi.spyOn(markdownIt.prototype, "parse").mockImplementation(function (
		this: unknown,
		...args
	) {
		const tokens = parse.apply(this, args);
		rewrite(tokens);
		return tokens;
	});
}

describe("markdown blocks", () => {
	it("renders every heading level as h4, below the view's own headings", () => {
		draw("# One\n\n### Three");
		expect([...root.querySelectorAll("h4")].map((h) => h.textContent)).toEqual([
			"One",
			"Three",
		]);
	});

	it("keeps an ordered list's start number", () => {
		// A numbered list split by a code block resumes at its own number; without
		// `start` it would restart at 1 and misnumber the steps.
		draw("3. three\n4. four");
		expect(root.querySelector("ol")?.getAttribute("start")).toBe("3");
		draw("1. one\n2. two");
		expect(root.querySelector("ol")?.hasAttribute("start")).toBe(false);
	});

	it("puts a tight list's text straight in the item, a loose list's in paragraphs", () => {
		draw("- a\n- b");
		expect(root.querySelector("li p")).toBeNull();
		draw("- a\n\n- b");
		expect(root.querySelectorAll("li p")).toHaveLength(2);
	});

	it("renders a table with its alignment as classes, never inline style", () => {
		// The page CSP blocks inline styles, and a class works under any host's.
		draw("| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |");
		expect(root.querySelector(".md-table table")).not.toBeNull();
		expect([...root.querySelectorAll("td")].map((td) => td.className)).toEqual([
			"align-left",
			"align-center",
			"align-right",
		]);
		expect(root.querySelector("[style]")).toBeNull();
	});

	it("renders a table that directly follows a line of text", () => {
		// Agent text often introduces a table with a line such as "Results:" and
		// no blank line before it.
		draw("Results:\n| a | b |\n|---|---|\n| 1 | 2 |");
		expect(root.querySelector("table")).not.toBeNull();
	});

	it("renders a fenced block without the language it names", () => {
		// `info` is agent-written; as a class it could apply any of the page's own
		// styles, and here it also tries to break out of the attribute.
		draw('```js" onload="x\nx < 1\n```');
		expect(root.querySelector("pre code")?.textContent).toBe("x < 1\n");
		expect(root.querySelector("pre code")?.hasAttribute("class")).toBe(false);
	});

	it("keeps a paragraph's single line breaks", () => {
		// Agent text uses single newlines as line breaks; collapsed to spaces, a
		// list of facts written one per line reads as one sentence.
		draw("first line\nsecond line");
		expect(root.querySelectorAll("p br")).toHaveLength(1);
	});

	it("renders every element of the subset", () => {
		draw(
			[
				"# h",
				"para **b** *i* ~~s~~ `c` https://example.org",
				"- tight\n  1. nested",
				"> quote",
				"---",
				"| t |\n|---|\n| d |",
				"```\nfence\n```",
				"[l](https://example.org) ![i](https://example.org/i.png)",
			].join("\n\n"),
		);
		expect(root.querySelector(".plain")).toBeNull();
		expect([...tags()].sort()).toEqual(
			[
				"A",
				"BLOCKQUOTE",
				"CODE",
				"DIV",
				"EM",
				"H4",
				"HR",
				"LI",
				"OL",
				"P",
				"PRE",
				"S",
				"SPAN",
				"STRONG",
				"TABLE",
				"TBODY",
				"TD",
				"TH",
				"THEAD",
				"TR",
				"UL",
			].sort(),
		);
	});
});

describe("markdown text that stays text", () => {
	it("keeps underscores literal, so a dunder name survives", () => {
		draw("see mitxonline/__main__.py and _not italic_");
		expect(root.querySelector("strong, em")).toBeNull();
		expect(text()).toBe("see mitxonline/__main__.py and _not italic_");
	});

	it("renders raw HTML as its source", () => {
		draw("<b>bold</b>\n\n<div onclick=x>block</div>");
		expect(root.querySelector("b, div[onclick]")).toBeNull();
		expect(text()).toContain("<b>bold</b>");
		expect(text()).toContain("<div onclick=x>block</div>");
	});

	it("keeps entity references as typed", () => {
		draw("&amp; &#106;");
		expect(text()).toBe("&amp; &#106;");
	});

	it("keeps a footnote visible, including one that cites a URL", () => {
		// A URL footnote is also a valid reference definition, which the parser
		// would otherwise consume, line and all.
		draw("see [^1]\n\n[^1]: https://github.com/mitodl/agent-kit");
		expect(text()).toContain("[^1]: https://github.com/mitodl/agent-kit");
	});

	it("renders a block type it does not know as its source", () => {
		withTokens((tokens) => {
			const open = tokens[0] as Token;
			open.type = "future_open";
		});
		draw("unknown block, kept");
		expect(root.querySelector(".plain")?.textContent).toBe(
			"unknown block, kept",
		);
	});

	it("renders an inline type it does not know as its text", () => {
		withTokens((tokens) => {
			const child = tokens[1]?.children?.[0] as Token;
			child.type = "future_inline";
		});
		draw("unknown inline, kept");
		expect(root.querySelector("p")?.textContent).toBe("unknown inline, kept");
	});

	it("renders input the parser cannot handle as plain text", () => {
		vi.spyOn(markdownIt.prototype, "parse").mockImplementation(() => {
			throw new Error("parser bug");
		});
		draw("**throws**");
		expect(root.querySelector(".plain")?.textContent).toBe("**throws**");
	});
});

describe("markdown links", () => {
	it("names the host after link text that is not the URL", () => {
		draw("[github.com/mitodl/hq](https://evil.example/x)");
		expect(root.querySelector("a")?.getAttribute("href")).toBe(
			"https://evil.example/x",
		);
		expect(root.querySelector(".md-host")?.textContent).toBe("(evil.example)");
	});

	it("names no host when the link text is the URL", () => {
		draw("[https://example.org/](https://example.org/)");
		expect(root.querySelector(".md-host")).toBeNull();
	});

	it.each([
		["a script URL", "[x](javascript:alert(1))", "javascript:"],
		[
			"a URL with credentials",
			"[x](https://github.com@evil.example)",
			"evil.example",
		],
		["an email address", "write to a@b.example", "mailto:"],
	])(
		"renders %s as its source, linking nowhere it names",
		(_, source, target) => {
			// The credential case still autolinks the `https://github.com` before the
			// `@`, as typed text; what must not exist is a link to the real target.
			draw(source);
			expect(root.querySelector(`a[href*="${target}"]`)).toBeNull();
			expect(text()).toBe(source);
		},
	);

	it("autolinks http(s) and www. URLs but not file names", () => {
		draw("https://example.org and www.example.org, not README.md");
		expect([...root.querySelectorAll("a")].map((a) => a.href)).toEqual([
			"https://example.org/",
			"http://www.example.org/",
		]);
		expect(text()).toContain("README.md");
	});

	it("shows an autolinked punycode host as typed, not decoded", () => {
		// The decoded form of this host is a Cyrillic look-alike of apple.com.
		draw("https://xn--pple-43d.com/login");
		expect(root.querySelector("a")?.textContent).toBe(
			"https://xn--pple-43d.com/login",
		);
	});

	it.each([
		[
			"a bare URL",
			"<https://github.com%2E%65%76%69%6C%2E%65%78%61%6D%70%6C%65/x>",
		],
		[
			"link text that copies it",
			"[https://github.com%2Eevil.example/x](https://github.com%2Eevil.example/x)",
		],
	])("shows the real host for a percent-encoded one in %s", (_, source) => {
		// The browser decodes the host; text that keeps it encoded reads as
		// `github.com` followed by noise while the link goes to evil.example.
		draw(source);
		expect(root.querySelector("a")?.getAttribute("href")).toBe(
			"https://github.com.evil.example/x",
		);
		expect(text()).toContain("github.com.evil.example");
	});

	it.each([
		[
			"plain link text",
			`[safe${RLO}txt.exe](https://example.org)`,
			"safetxt.exe",
		],
		[
			"code in link text",
			`[\`safe${RLO}txt.exe\`](https://example.org)`,
			"safetxt.exe",
		],
		[
			"image alt text",
			`![safe${RLO}txt.exe](https://example.org/i.png)`,
			"image safetxt.exe",
		],
		[
			"link text past the walker's depth",
			`${"> ".repeat(18)}[safe${RLO}txt.exe](https://example.org)`,
			"safetxt.exe",
		],
	])("strips bidi controls from %s", (_, source, shown) => {
		draw(source);
		expect(root.querySelector("a")?.textContent).toBe(shown);
	});
});

describe("markdown images", () => {
	it("renders an image as a marked link with its alt text and host, never an img", () => {
		draw("![the board](https://example.org/b.png)");
		expect(root.querySelector("img")).toBeNull();
		const link = root.querySelector("a");
		expect(link?.getAttribute("href")).toBe("https://example.org/b.png");
		expect(link?.querySelector(".md-image")).not.toBeNull();
		expect(link?.textContent).toContain("the board");
		expect(root.querySelector(".md-host")?.textContent).toBe("(example.org)");
	});

	it("keeps underscores literal in alt text", () => {
		draw("![__main__.py output](https://example.org/b.png)");
		expect(root.querySelector("a")?.textContent).toContain(
			"__main__.py output",
		);
	});

	it("names an image with no alt text by its URL", () => {
		draw("![](https://example.org/b.png)");
		expect(root.querySelector("a")?.textContent).toContain(
			"https://example.org/b.png",
		);
	});

	it("renders an image inside a link without a second anchor", () => {
		// A CI badge: anchors cannot nest, and the click belongs to the outer link.
		draw("[![build](https://ci.example/badge.svg)](https://example.org/ci)");
		const links = root.querySelectorAll("a");
		expect(links).toHaveLength(1);
		expect(links[0]?.getAttribute("href")).toBe("https://example.org/ci");
		expect(links[0]?.querySelector(".md-image")).not.toBeNull();
	});
});

describe("markdown limits", () => {
	it("renders input up to 64KB, and anything larger as plain text", () => {
		const fits = `**bold** ${"x".repeat(64 * 1024 - 9)}`;
		draw(fits);
		expect(root.querySelector("strong")).not.toBeNull();
		const over = `${fits}x`;
		draw(over);
		expect(root.querySelector("strong")).toBeNull();
		expect(root.querySelector(".plain")?.textContent).toBe(over);
	});

	it("renders tables past the document's budget as plain text", () => {
		// markdown-it fills in empty cells up to a cap per table, so many wide
		// tables of bare `|` rows expand into millions of cells; one table this
		// size alone is within budget.
		const table = `${"|".repeat(257)}\n${"|-".repeat(256)}|\n${"|\n".repeat(256)}`;
		const source = Array(42).fill(table).join("\n");
		expect(source.length).toBeLessThan(64 * 1024);
		draw(source);
		expect(root.querySelector("table")).toBeNull();
		expect(root.querySelector(".plain")?.textContent).toBe(source);
	});

	it("renders input nested past the parser's limit as plain text", () => {
		// Past `maxNesting` markdown-it drops the rest of the document silently.
		const source = `${"> ".repeat(150)}deep\n\nafter`;
		draw(source);
		expect(root.querySelector(".plain")?.textContent).toBe(source);
	});

	it("renders a subtree nested past the walker's depth as its text", () => {
		draw(`${"> ".repeat(30)}deep`);
		expect(root.querySelectorAll("blockquote").length).toBeLessThan(30);
		expect(text()).toContain("deep");
	});
});

describe("markdown invariants", () => {
	// The non-test sources, as text. The hostile-input test below pins what the
	// renderer emits; this pins that nothing in the UI can emit markup from a
	// string at all, which is the property the renderer's safety rests on.
	const SOURCES = import.meta.glob(["./**/*.ts", "!./**/*.test.ts"], {
		query: "?raw",
		import: "default",
		eager: true,
	}) as Record<string, string>;

	const SINKS = [
		/\bunsafe(HTML|SVG|MathML|Static)\b/,
		/lit-html\/static/,
		/\b(inner|outer)HTML\b/,
		/\b(setHTMLUnsafe|parseHTMLUnsafe)\b/,
		/\binsertAdjacentHTML\b/,
		/\bDOMParser\b/,
		/\bcreateContextualFragment\b/,
		/\bdocument\.write/,
		/\bsrcdoc\b/,
		// markdown-it's own HTML output.
		/\.render(Inline)?\(/,
	];

	/** Comments removed. A line comment starts a line or follows whitespace, so
	 * the `//` inside a URL literal survives. */
	function code(source: string): string {
		return source
			.replace(/\/\*[\s\S]*?\*\//g, "")
			.replace(/(^|\s)\/\/.*$/gm, "$1");
	}

	it("finds the sources it checks", () => {
		expect(Object.keys(SOURCES)).toContain("./markdown.ts");
	});

	it.each(SINKS.map((sink) => [String(sink), sink] as const))(
		"no source uses %s",
		(_, sink) => {
			const hits = Object.entries(SOURCES)
				.filter(([, source]) => sink.test(code(source)))
				.map(([path]) => path);
			expect(hits).toEqual([]);
		},
	);

	const ELEMENTS = new Set([
		"P",
		"H4",
		"UL",
		"OL",
		"LI",
		"BLOCKQUOTE",
		"HR",
		"DIV",
		"TABLE",
		"THEAD",
		"TBODY",
		"TR",
		"TH",
		"TD",
		"PRE",
		"CODE",
		"BR",
		"STRONG",
		"EM",
		"S",
		"A",
		"SPAN",
	]);

	const CLASSES = new Set([
		"plain",
		"md-table",
		"md-image",
		"md-host",
		"align-left",
		"align-center",
		"align-right",
	]);

	// Raw HTML needs one row only: the walker renders HTML tokens as text, so
	// more would test the same branch.
	const HOSTILE = [
		"<iframe srcdoc='<script>x</script>'></iframe>",
		"[x](javascript:alert(1)) [y](JAVASCRIPT:alert(1)) [z](java\tscript:x)",
		"[x](data:text/html,<script>x</script>) [y](vbscript:x) [z](file:///etc)",
		'[x](https://a.example "t\\" onmouseover=\\"x")',
		'![x](javascript:alert(1)) ![y](https://a.example/i.png"onerror=x)',
		"<javascript:alert(1)> <https://u:p@a.example>",
		"| a |\n|:-:|\n| b |",
		"7. seventh\n8. eighth",
		`${"- ".repeat(60)}x`,
		`${"[".repeat(500)}x${"](https://a.example)".repeat(500)}`,
		"[x](  https://a.example/\u0000 )",
	];

	it.each(HOSTILE)("emits only allowed markup for %j", (source) => {
		draw(source);
		for (const element of root.querySelectorAll("*")) {
			expect(ELEMENTS).toContain(element.tagName);
			for (const { name, value } of element.attributes) {
				expect(["href", "start", "class"]).toContain(name);
				if (name === "class") {
					for (const token of value.split(/\s+/)) {
						expect(CLASSES).toContain(token);
					}
				}
				if (name === "href") {
					// Checked directly rather than through `isLinkable`, so a bug in
					// the renderer's link rule cannot pass its own test.
					const url = new URL(value);
					expect(["http:", "https:"]).toContain(url.protocol);
					expect(url.username + url.password).toBe("");
				}
				if (name === "start") {
					expect(Number.isSafeInteger(Number(value))).toBe(true);
				}
			}
		}
	});
});
