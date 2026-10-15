import { html, nothing, type TemplateResult } from "lit-html";
import markdownIt, { type MarkdownIt, type Token } from "markdown-it";
import { isLinkable } from "./format.js";

/**
 * Agent-written prose rendered as a fixed subset of markdown.
 *
 * ★ NO HTML STRING IS EVER BUILT OR PARSED. markdown-it is used as a parser
 * only: its token stream is walked here and each allowed token type is mapped
 * to a lit-html template, so every piece of text reaches the DOM through a
 * `${}` text binding and lit's escaping is the only escaping there is.
 * markdown-it's own HTML renderer would hand back a string that needs a
 * sanitizer to be safe; the page holds a token that authorizes write tools
 * (`ui_routes.py`), so an injection here is a write exposure, not a defaced
 * page. The CSP is a second layer and not one this can lean on: a widget
 * renders under its host's CSP, not witan's.
 *
 * Anything the walker does not know renders as its own text, so a parser
 * upgrade that adds a token type degrades to text rather than dropping
 * content. Images render as links, never `<img>`: loading one is a request
 * nobody clicked, which tells whoever wrote the URL who read the page.
 */

/**
 * Above this the field renders as plain text. About five times the largest
 * prose field measured in the shared graph (12,054 characters, a memory, on
 * 2026-10-14); markdown-it's worst case on pathological input at this size is
 * on the order of 100ms, which is the cost of a field nobody can read anyway.
 */
const MAX_SOURCE = 64 * 1024;

/**
 * Deeper than this, a subtree renders as its text. Counted by the walker's
 * own recursion, so neither the walk nor lit's nested render can exhaust the
 * stack on input markdown-it accepts.
 */
const MAX_DEPTH = 20;

/**
 * Table tokens one field may produce before it renders as plain text.
 * markdown-it caps the empty cells it fills in per table, not per document, so
 * 42 wide tables of bare `|` rows fit in 54KB and parse into 8.3 million
 * tokens (2.6s and 1.3GB of heap in Node). A large real table is a few
 * thousand tokens.
 */
const MAX_TABLE_TOKENS = 50_000;

/** Parsed sources kept, least recently used dropped first, for the 30-second
 * poll that re-renders every view. */
const MEMO_SIZE = 200;

const BIDI_CONTROLS = /[\u202A-\u202E\u2066-\u2069]/g;

const ALIGN_CLASSES: Record<string, string> = {
	"text-align:left": "align-left",
	"text-align:center": "align-center",
	"text-align:right": "align-right",
};

/** `null` records a source over the table budget, so it is not re-parsed. */
const memo = new Map<string, Token[] | null>();

class OverBudget extends Error {}
let parser: MarkdownIt | undefined;

/**
 * The URL to put in an `href`, or `null` when there must be no link.
 *
 * `isLinkable` is the rule every other link in the page follows (http and
 * https only). Credentials are refused as well because they are how a URL
 * reads as one host and goes to another (`https://github.com@evil.example`).
 * The normalized `URL.href` is what gets emitted, so the string that was
 * checked is the string the browser follows.
 */
export function safeHref(raw: string): string | null {
	if (!isLinkable(raw)) {
		return null;
	}
	const url = new URL(raw);
	return url.username || url.password ? null : url.href;
}

/**
 * Created on first use, not at import, so a bundle that imports a view but
 * never renders prose does not construct a parser it will not call.
 */
function md(): MarkdownIt {
	if (!parser) {
		parser = markdownIt({ html: false, linkify: true });
		// `www.example.org` autolinks only with fuzzy links on. They also turn
		// `README.md` into `http://README.md`; `autolink()` undoes that.
		parser.linkify.set({ fuzzyLink: true });
		// Entities stay as typed. Reference definitions are off because a
		// footnote that cites a URL (`[^1]: https://…`) is also a valid one, and
		// the parser would swallow the line; with them off it stays as text.
		parser.disable(["entity", "reference"]);
		// A link, image or bare URL that fails this stays in the token stream as
		// its literal source text, which is the fallback the spec wants.
		parser.validateLink = (url) => safeHref(url) !== null;
		// Throws as soon as the document's tables pass the budget, so a hostile
		// field costs at most one table past it; checking the token count after
		// `parse` would come after the allocation it exists to prevent.
		const ruler = parser.block.ruler;
		const rule = ruler.__rules__[ruler.__find__("table")];
		const table = rule?.fn;
		if (rule && table) {
			// `alt` passed again: `at` resets the rule's alt chain otherwise, and
			// without `paragraph` in it a table can no longer follow a line of text.
			ruler.at(
				"table",
				(state, startLine, endLine, silent) => {
					const before = state.tokens.length;
					const matched = table(state, startLine, endLine, silent);
					const env = state.env as { tableTokens?: number };
					env.tableTokens =
						(env.tableTokens ?? 0) + state.tokens.length - before;
					if (env.tableTokens > MAX_TABLE_TOKENS) {
						throw new OverBudget();
					}
					return matched;
				},
				{ alt: rule.alt },
			);
		}
	}
	return parser;
}

function parse(source: string): Token[] | null {
	const cached = memo.get(source);
	if (cached !== undefined) {
		// Re-inserted so a field read on every poll is never the one evicted.
		memo.delete(source);
		memo.set(source, cached);
		return cached;
	}
	let tokens: Token[] | null;
	try {
		tokens = md().parse(source, {});
	} catch (error) {
		if (!(error instanceof OverBudget)) {
			throw error;
		}
		tokens = null;
	}
	if (memo.size >= MEMO_SIZE) {
		const oldest = memo.keys().next().value;
		if (oldest !== undefined) {
			memo.delete(oldest);
		}
	}
	memo.set(source, tokens);
	return tokens;
}

/** The field as plain text: one paragraph, its line breaks kept. */
function plain(source: string): TemplateResult {
	return html`<p class="plain">${source}</p>`;
}

/**
 * Render `source` as block content for the caller's `.prose` container.
 *
 * Oversized input, tables past their budget, input markdown-it nests past its
 * own limit, and anything that throws render as plain text.
 */
export function markdown(source: string): TemplateResult {
	if (source.length > MAX_SOURCE) {
		return plain(source);
	}
	try {
		const tokens = parse(source);
		if (!tokens) {
			return plain(source);
		}
		// Past `maxNesting` markdown-it silently drops the rest of the document;
		// an open token at the last allowed level is the only trace it leaves.
		const limit = (md().options.maxNesting ?? 100) - 1;
		if (tokens.some((token) => token.level >= limit)) {
			return plain(source);
		}
		const lines = source.split("\n");
		return html`${tree(tokens).map((node) => block(node, lines, 0))}`;
	} catch {
		return plain(source);
	}
}

/** A token with whatever it encloses, rebuilt from the flat open/close pairs. */
interface Node {
	token: Token;
	children: Node[];
}

/** Built with a stack rather than recursion, so depth cannot overflow it. */
function tree(tokens: Token[]): Node[] {
	const root: Node[] = [];
	const stack: Node[][] = [root];
	for (const token of tokens) {
		const siblings = stack[stack.length - 1] ?? root;
		if (token.nesting === -1) {
			if (stack.length > 1) {
				stack.pop();
			}
			continue;
		}
		const node: Node = { token, children: [] };
		siblings.push(node);
		if (token.nesting === 1) {
			stack.push(node.children);
		}
	}
	return root;
}

/**
 * Every piece of text under `nodes`, for a subtree rendered as text.
 *
 * Underscore emphasis keeps its delimiters here too, as it does in
 * `inlineNode`: image alt text is always flattened, and `__main__` there would
 * otherwise lose its underscores.
 */
function textOf(nodes: Node[]): string {
	let out = "";
	const pending: (Node | string)[] = [...nodes].reverse();
	while (pending.length) {
		const node = pending.pop() as Node | string;
		if (typeof node === "string") {
			out += node;
			continue;
		}
		const { token } = node;
		if (token.nesting === 1 && token.markup.startsWith("_")) {
			out += token.markup;
			pending.push(token.markup);
		}
		if (token.type === "softbreak" || token.type === "hardbreak") {
			out += "\n";
		} else if (token.children) {
			pending.push(...tree(token.children).reverse());
		} else {
			out += token.content;
		}
		pending.push(...[...node.children].reverse());
	}
	return out;
}

/** A block's own source lines, where markdown-it recorded them. */
function sourceOf(node: Node, lines: string[]): string {
	const map = node.token.map;
	return map ? lines.slice(map[0], map[1]).join("\n") : textOf([node]);
}

function block(node: Node, lines: string[], depth: number): unknown {
	const { token } = node;
	if (depth > MAX_DEPTH) {
		return plain(sourceOf(node, lines));
	}
	const inner = () =>
		node.children.map((child) => block(child, lines, depth + 1));
	switch (token.type) {
		case "inline":
			return inline(tree(token.children ?? []), depth + 1);
		case "paragraph_open":
			// A tight list's paragraphs are hidden: their text sits directly in
			// the `<li>`, which is what keeps the list from spacing out.
			return token.hidden ? inner() : html`<p>${inner()}</p>`;
		case "heading_open":
			// One level for every heading, below whatever heading the view drew.
			return html`<h4>${inner()}</h4>`;
		case "bullet_list_open":
			return html`<ul>${inner()}</ul>`;
		case "ordered_list_open":
			return html`<ol start=${listStart(token)}>${inner()}</ol>`;
		case "list_item_open":
			return html`<li>${inner()}</li>`;
		case "blockquote_open":
			return html`<blockquote>${inner()}</blockquote>`;
		case "table_open":
			return html`<div class="md-table"><table>${inner()}</table></div>`;
		case "thead_open":
			return html`<thead>${inner()}</thead>`;
		case "tbody_open":
			return html`<tbody>${inner()}</tbody>`;
		case "tr_open":
			return html`<tr>${inner()}</tr>`;
		case "th_open":
			return html`<th class=${alignClass(token)}>${inner()}</th>`;
		case "td_open":
			return html`<td class=${alignClass(token)}>${inner()}</td>`;
		case "fence":
		case "code_block":
			// No language class: `info` is free text an agent wrote, and a class
			// from it would let prose apply any of the page's own styles.
			return html`<pre><code>${token.content}</code></pre>`;
		case "hr":
			return html`<hr />`;
		default:
			return plain(token.nesting === 1 ? sourceOf(node, lines) : token.content);
	}
}

function inline(nodes: Node[], depth: number, inLink = false): unknown[] {
	return nodes.map((node) => inlineNode(node, depth, inLink));
}

function inlineNode(node: Node, depth: number, inLink: boolean): unknown {
	const { token } = node;
	if (depth > MAX_DEPTH) {
		return flat(node, inLink);
	}
	const inner = () => inline(node.children, depth + 1, inLink);
	switch (token.type) {
		case "text":
			return inLink ? token.content.replace(BIDI_CONTROLS, "") : token.content;
		// Soft breaks render as breaks: agent text uses single newlines as line
		// breaks, and run together its lists of facts read as one sentence.
		case "softbreak":
		case "hardbreak":
			return html`<br />`;
		case "strong_open":
		case "em_open":
			// Underscores are literal. Agent text names Python dunders without
			// backticks (`mitxonline/__main__.py`), which CommonMark reads as bold
			// and renders with the underscores gone; emphasis is `*`/`**` only.
			if (token.markup.startsWith("_")) {
				return [token.markup, inner(), token.markup];
			}
			return token.type === "strong_open"
				? html`<strong>${inner()}</strong>`
				: html`<em>${inner()}</em>`;
		case "s_open":
			return html`<s>${inner()}</s>`;
		case "code_inline":
			return html`<code
				>${inLink ? token.content.replace(BIDI_CONTROLS, "") : token.content}</code
			>`;
		case "link_open":
			return link(node, depth);
		case "image":
			return image(token, inLink);
		default:
			return flat(node, inLink);
	}
}

/**
 * A subtree rendered as its text. Inside a link the bidi controls go, as they
 * do from the label's own text: past the depth limit or for an unknown token,
 * the label still sits in the anchor.
 */
function flat(node: Node, inLink: boolean): string {
	const text = textOf([node]);
	return inLink ? text.replace(BIDI_CONTROLS, "") : text;
}

function link(node: Node, depth: number): unknown {
	const { token } = node;
	const raw = attr(token, "href");
	const href = safeHref(raw);
	const text = textOf(node.children);
	// `validateLink` has already turned a rejected URL back into text; checked
	// again so no href reaches the DOM on the parser's configuration alone.
	if (!href) {
		return text;
	}
	if (token.markup === "linkify" || token.markup === "autolink") {
		return autolink(raw, href, text);
	}
	// Compared with the URL the browser follows, not markdown-it's form of it:
	// markdown-it keeps a percent-encoded host (`github.com%2Eevil.example`)
	// that the browser decodes, so text can match `raw` and still name a host
	// the link does not go to.
	const self = text === href;
	return html`<a href=${href}
			>${inline(node.children, depth + 1, true)}</a
		>${self ? nothing : host(href)}`;
}

/**
 * A bare URL, shown as the `href` the browser follows. markdown-it's display
 * text decodes punycode, so `xn--pple-43d.com` would show as a Cyrillic
 * look-alike of `apple.com`; its own `href` keeps a percent-encoded host the
 * browser decodes. `URL.href` has neither problem: hosts are ASCII and
 * decoded, so what the link says is where it goes.
 */
function autolink(raw: string, href: string, text: string): unknown {
	// Fuzzy links are on for `www.`; they also catch file names with a
	// country-code extension (`README.md`, `config.py`), which stay text.
	if (!/^(https?:\/\/|www\.)/i.test(text)) {
		return text;
	}
	// `URL.href` percent-encodes bidi controls; `.prose a` isolates the link
	// from any in the text around it.
	// `URL.href` gives a bare origin a path of `/`; dropped again when the
	// source had none, so `https://github.com.` does not read as `…com/.`.
	const added = href.endsWith("/") && !raw.endsWith("/") && !raw.includes("?");
	return html`<a href=${href}>${added ? href.slice(0, -1) : href}</a>`;
}

/**
 * An image is a link to it, marked as one, with its host. Inside a link (a CI
 * badge, `[![build](badge.svg)](ci)`) it is the marker and alt text only: an
 * anchor cannot nest, and the click belongs to the outer link.
 */
function image(token: Token, inLink: boolean): unknown {
	const href = safeHref(attr(token, "src"));
	// The alt text is the token's children; its `alt` attribute is always "".
	const alt = textOf(tree(token.children ?? [])).replace(BIDI_CONTROLS, "");
	if (!href) {
		return alt;
	}
	if (inLink) {
		return html`<span class="md-image">image</span> ${alt || href}`;
	}
	return html`<a href=${href}
			><span class="md-image">image</span> ${alt || href}</a
		>${host(href)}`;
}

/** Where a link really goes, for link text that names somewhere else. */
function host(href: string): TemplateResult {
	return html` <span class="md-host">(${new URL(href).hostname})</span>`;
}

function listStart(token: Token): number | typeof nothing {
	// markdown-it sets `start` only on a list that does not begin at 1, and
	// `Number(null)` is 0, so the absent case is checked before converting.
	const raw = token.attrGet("start");
	const start = raw === null ? Number.NaN : Number(raw);
	return Number.isSafeInteger(start) ? start : nothing;
}

function alignClass(token: Token): string | typeof nothing {
	return ALIGN_CLASSES[attr(token, "style")] ?? nothing;
}

function attr(token: Token, name: string): string {
	return String(token.attrGet(name) ?? "");
}
