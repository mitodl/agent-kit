import { html, nothing, type TemplateResult } from "lit-html";
import { absolute, ago, repoLabel } from "../format.js";
import type { Snapshot } from "../live.js";
import { markdown } from "../markdown.js";
import {
	DEFAULT_ROUTE,
	MEMORY_FACETS,
	type MemoryFacet,
	type Route,
	routeHref,
} from "../route.js";
import {
	type Contradiction,
	type EdgeMeta,
	type Memory,
	type MemoryContradiction,
	type MemoryKind,
	type MemoryNeighbor,
	type MemoryNeighbors,
	NEIGHBOR_KINDS,
	type NeighborKind,
	type RecallResult,
	type ResolvedSymbolRef,
	type StaleMemory,
	type StaleRefsReport,
	type Topic,
} from "../types.js";

/**
 * The memory view (spec §6.7): a contradictions inbox, browse and search, and
 * one memory's neighbourhood in the panel.
 *
 * ★ THE INBOX IS THE LANDING STATE. `recall` computes contradictions on every
 * call and nothing rendered them, so a Contradicts pair, which the schema
 * promises is never hidden and always surfaced for review, was in practice
 * reviewed by nobody. The view opens on every pair in scope, both bodies side
 * by side, before it offers anything else.
 */

/**
 * `memory_list`'s row cap (server.py `memory_list`). It excludes superseded
 * memories before capping, so a browse list this long may be missing older
 * ones and a shorter one is everything in scope.
 */
export const MEMORY_LIST_CAP = 100;

/**
 * The most rows a topic page draws. A topic read holds the whole topic, so
 * the Kind select and the facets narrow all of it before this cuts the list.
 */
export const TOPIC_ROW_CAP = 500;

/** What the memory view's main area renders from. One read per route. */
export type MemoryPage =
	| {
			mode: "browse";
			memories: Memory[];
			/** Every unresolved pair in scope, from `memory_contradictions`. */
			inbox: MemoryContradiction[];
	  }
	| {
			mode: "recall";
			memories: Memory[];
			/**
			 * `recall`'s own pairs, which are only the ones with BOTH sides in its
			 * result and at least one side in the repo filter, the inbox's rule.
			 * So every pair here is also in the inbox for the same repo, even
			 * when expansion pulled both memories in from another repo. Shown as
			 * recall reports them rather than joined against the inbox, so this
			 * view and an agent calling `recall` see the same thing.
			 */
			pairs: Contradiction[];
	  }
	| { mode: "plain"; memories: Memory[] }
	| {
			mode: "topic";
			/** `null` when no such Topic exists: a stale link, not a failure. */
			topic: Topic | null;
			/** Every memory of the selected kind, before the facets and the row cap. */
			memories: Memory[];
			/** The read came back at its limit, so memories may be missing. */
			readAtLimit: boolean;
	  };

/** What the panel renders from for a memory slug. */
export interface MemoryPanel {
	memory: Memory | null;
	neighbors: MemoryNeighbors;
}

const MEMORY_SLUG = /^(pat|pf|les|ctx|mem)-/;

/**
 * Whether the panel's slug is a memory rather than a task.
 *
 * On the prefix, which witan mints per kind (`_KIND_PREFIX` in server.py,
 * plus its `mem` fallback), so a memory linked from anywhere opens as one.
 */
export function isMemorySlug(slug: string): boolean {
	return MEMORY_SLUG.test(slug);
}

const KIND_LABELS: Record<MemoryKind, string> = {
	pattern: "pattern",
	project_fact: "project fact",
	lesson: "lesson",
	agent_context: "agent context",
};

const NEIGHBOR_LABELS: Record<NeighborKind, string> = {
	superseded_by: "Superseded by",
	supersedes: "Supersedes",
	refines: "Refines",
	applies_to: "Applies to",
	related_to: "Related to",
	contradicts: "Contradicts",
};

const FACET_LABELS: Record<MemoryFacet, string> = {
	language: "Language",
	category: "Category",
	severity: "Severity",
	tag: "Tag",
	author: "Author",
};

/** The values one memory has for a facet. Tags are the only multi-valued one. */
function facetValues(memory: Memory, facet: MemoryFacet): string[] {
	if (facet === "tag") {
		return memory.tags ?? [];
	}
	const value = memory[facet];
	return value ? [value] : [];
}

/** The memories that match every set facet. */
export function applyFacets(
	memories: Memory[],
	facets: Route["facets"],
): Memory[] {
	return memories.filter((memory) =>
		MEMORY_FACETS.every((facet) => {
			const wanted = facets[facet];
			return wanted === null || facetValues(memory, facet).includes(wanted);
		}),
	);
}

/**
 * The options each facet offers, from what the read returned.
 *
 * ★ FROM THE UNFILTERED LIST, not the filtered one. Deriving them after
 * filtering would leave each select offering only the value already chosen,
 * with no way to change it short of clearing it first.
 */
export function facetOptions(
	memories: Memory[],
): Record<MemoryFacet, string[]> {
	return Object.fromEntries(
		MEMORY_FACETS.map((facet) => [
			facet,
			[...new Set(memories.flatMap((m) => facetValues(m, facet)))].sort(),
		]),
	) as Record<MemoryFacet, string[]>;
}

// ── Stale symbol refs ──────────────────────────────────────────────

/**
 * `memory_stale_refs`'s flagged memories by slug.
 *
 * ★ ONLY `stale` IS A MARK. A memory whose refs are merely `unknown` (its
 * repo has no index, or witan-code is absent) is in the report too, and
 * marking it would flag every memory in a repo nobody indexed. Those are
 * counted in the summary note and explained per ref in the panel instead.
 */
export function staleBySlug(
	report: StaleRefsReport | null,
): Map<string, StaleMemory> {
	return new Map(report?.memories.map((memory) => [memory.slug, memory]));
}

function hasMissingRef(entry: StaleMemory | undefined): boolean {
	return entry?.status === "stale";
}

/** The marker's tooltip: which refs are gone, and where each may have gone. */
function staleTitle(entry: StaleMemory): string {
	return entry.refs
		.filter((ref) => ref.status === "missing")
		.map((ref) =>
			ref.candidates?.length
				? `${ref.symbol_ref} is not in the code graph; same name at ${ref.candidates.join(", ")}`
				: `${ref.symbol_ref} is not in the code graph`,
		)
		.join("\n");
}

/**
 * What the stale check found, or why it has not: drawn above the table.
 *
 * Without a report no row is marked, so the note says the check is pending or
 * failed rather than letting an unmarked list read as "every ref resolves".
 */
function staleNote(
	stale: Snapshot<StaleRefsReport> | null,
	route: Route,
): TemplateResult | typeof nothing {
	const report = stale?.data ?? null;
	if (report === null) {
		if (stale?.error) {
			return html`<p class="stale-summary" title=${stale.error.message}>
        Could not check symbol refs against the code graph, so no memory is
        marked stale.
      </p>`;
		}
		return route.stale
			? html`<p class="stale-summary" aria-busy="true">
            Checking symbol refs against the code graph…
          </p>`
			: nothing;
	}
	const stales = report.memories.filter((m) => m.status === "stale").length;
	const unknown = report.memories.filter((m) => m.status === "unknown").length;
	if (stales === 0 && unknown === 0) {
		return nothing;
	}
	// The report names the scope it checked, which a topic or a recall widens
	// past the repo filter.
	const scope = report.repo ? repoLabel(report.repo) : "any repo";
	return html`<p class="stale-summary">
    ${
			stales
				? html`${stales} ${stales === 1 ? "memory" : "memories"} in ${scope}
          ${stales === 1 ? "cites" : "cite"} a symbol the code graph no longer
          has, marked <span class="badge stale-refs">stale refs</span>.`
				: nothing
		}
    ${
			unknown
				? html`${unknown} ${unknown === 1 ? "memory has" : "memories have"}
          refs that could not be checked (e.g. the repo is not indexed) and
          none that are missing, so ${unknown === 1 ? "it is" : "they are"} not
          marked; the memory's panel says why.`
				: nothing
		}
  </p>`;
}

/**
 * Stale memories the browse list does not hold.
 *
 * `memory_list` is capped at the most recent `MEMORY_LIST_CAP`, and older
 * memories are the likeliest to cite a symbol that has since moved, so the
 * filter alone would hide exactly the ones it exists for. The report carries
 * each one's title, which is enough to link it.
 */
function staleOutsideList(
	page: MemoryPage,
	route: Route,
	report: StaleRefsReport | null,
): TemplateResult | typeof nothing {
	if (page.mode !== "browse" || report === null) {
		return nothing;
	}
	const loaded = new Set(page.memories.map((memory) => memory.slug));
	const outside = report.memories.filter(
		(memory) =>
			memory.status === "stale" &&
			(!route.kind || memory.kind === route.kind) &&
			!loaded.has(memory.slug),
	);
	if (outside.length === 0) {
		return nothing;
	}
	// The report has no language, category, severity, tag or author, so the
	// facets cannot narrow these; say so rather than imply they did.
	const faceted = MEMORY_FACETS.some((facet) => route.facets[facet] !== null);
	return html`<div class="stale-summary stale-outside">
    <p>
      ${outside.length} stale ${outside.length === 1 ? "memory is" : "memories are"}
      older than this list reaches${faceted ? ", not narrowed by the facets above" : ""}:
    </p>
    <ul>
      ${outside.map(
				(memory) =>
					html`<li>
            <a href=${routeHref(route, { slug: memory.slug })}>${memory.title}</a>
            <span class="badge">${KIND_LABELS[memory.kind]}</span>
          </li>`,
			)}
    </ul>
  </div>`;
}

// ── The main area ──────────────────────────────────────────────────

export function memoryView(
	page: MemoryPage,
	route: Route,
	onNavigate: (patch: Partial<Route>) => void,
	stale: Snapshot<StaleRefsReport> | null = null,
	now = Date.now(),
): TemplateResult {
	const flagged = staleBySlug(stale?.data ?? null);
	const faceted = applyFacets(page.memories, route.facets);
	const matching = route.stale
		? faceted.filter((memory) => hasMissingRef(flagged.get(memory.slug)))
		: faceted;
	const shown =
		page.mode === "topic" ? matching.slice(0, TOPIC_ROW_CAP) : matching;
	return html`
    <div class="memory-view">
      ${toolbar(route, facetOptions(page.memories), onNavigate)}
      ${page.mode === "browse" ? inbox(page.inbox, route, now) : nothing}
      ${page.mode === "recall" ? recallPairs(page.pairs, page.memories, route) : nothing}
      ${page.mode === "topic" ? topicHeading(page.topic, route) : nothing}
      ${staleNote(stale, route)}
      ${staleOutsideList(page, route, stale?.data ?? null)}
      ${limitNotes(page, matching.length)}
      ${
				page.mode === "topic" && page.topic === null
					? nothing
					: memoryTable(page, shown, route, now, flagged, stale?.data != null)
			}
    </div>
  `;
}

/**
 * Why a list may be missing memories, drawn above the table so a filtered
 * empty state carries it too.
 */
function limitNotes(page: MemoryPage, matching: number): TemplateResult[] {
	const notes: TemplateResult[] = [];
	if (page.mode === "browse" && page.memories.length >= MEMORY_LIST_CAP) {
		notes.push(html`<p class="note">
      The list came back at its ${MEMORY_LIST_CAP}-memory limit, so older
      memories may not be shown. Narrow by kind, or search.
    </p>`);
	}
	if (page.mode === "topic" && page.readAtLimit) {
		notes.push(html`<p class="note">
      The topic's read came back at its limit, so lower-ranked memories may
      not be shown, whatever the filters.
    </p>`);
	}
	if (page.mode === "topic" && matching > TOPIC_ROW_CAP) {
		notes.push(html`<p class="note">
      Showing the first ${TOPIC_ROW_CAP} of ${matching} memories. Narrow by
      kind or a facet to see the rest.
    </p>`);
	}
	return notes;
}

function toolbar(
	route: Route,
	options: Record<MemoryFacet, string[]>,
	onNavigate: (patch: Partial<Route>) => void,
): TemplateResult {
	return html`
    <form
      class="memory-toolbar"
      role="search"
      @submit=${(event: SubmitEvent) => {
				event.preventDefault();
				const form = event.currentTarget as HTMLFormElement;
				const q = new FormData(form).get("q");
				// A search replaces a topic rather than narrowing it: `topicGet` takes
				// no query, so keeping both would show a search box that does nothing.
				onNavigate({ q: typeof q === "string" ? q.trim() : "", topic: null });
			}}
    >
      <input
        type="search"
        name="q"
        aria-label="Search memories"
        placeholder="Search memories"
        .value=${route.q}
      />
      <button type="submit">Search</button>
      <label>
        Kind
        <select
          @change=${(event: Event) =>
						onNavigate({
							kind:
								((event.target as HTMLSelectElement).value as MemoryKind) ||
								null,
						})}
        >
          <option value="">All kinds</option>
          ${Object.entries(KIND_LABELS).map(
						([kind, label]) =>
							html`<option value=${kind} ?selected=${kind === route.kind}>
                ${label}
              </option>`,
					)}
        </select>
      </label>
      ${MEMORY_FACETS.map((facet) =>
				facetSelect(facet, options[facet], route, onNavigate),
			)}
      <label title="memory_search: BM25 only, no graph expansion or re-ranking">
        <input
          type="checkbox"
          .checked=${route.plain}
          @change=${(event: Event) =>
						onNavigate({ plain: (event.target as HTMLInputElement).checked })}
        />
        Plain search
      </label>
      <label
        title="Memories citing a symbol whose repo is indexed and the symbol is not. A ref in an unindexed repo does not count."
      >
        <input
          type="checkbox"
          .checked=${route.stale}
          @change=${(event: Event) =>
						onNavigate({ stale: (event.target as HTMLInputElement).checked })}
        />
        Stale refs only
      </label>
      ${
				// `topicGet` always includes superseded memories, so in a topic the
				// toggle would be a control that changes nothing.
				route.topic
					? nothing
					: html`<label>
              <input
                type="checkbox"
                .checked=${route.superseded}
                @change=${(event: Event) =>
									onNavigate({
										superseded: (event.target as HTMLInputElement).checked,
									})}
              />
              Show superseded
            </label>`
			}
    </form>
  `;
}

/**
 * One facet's select, or nothing when the list has no values for it.
 *
 * A facet that IS set always renders, even with no options, so a link that
 * narrows to a value the current read lacks can still be cleared.
 */
function facetSelect(
	facet: MemoryFacet,
	values: string[],
	route: Route,
	onNavigate: (patch: Partial<Route>) => void,
): TemplateResult | typeof nothing {
	const selected = route.facets[facet];
	const choices =
		selected && !values.includes(selected) ? [selected, ...values] : values;
	if (choices.length === 0) {
		return nothing;
	}
	return html`
    <label>
      ${FACET_LABELS[facet]}
      <select
        @change=${(event: Event) =>
					onNavigate({
						facets: {
							...route.facets,
							[facet]: (event.target as HTMLSelectElement).value || null,
						},
					})}
      >
        <option value="">Any</option>
        ${choices.map(
					(value) =>
						html`<option value=${value} ?selected=${value === selected}>
              ${value}
            </option>`,
				)}
      </select>
    </label>
  `;
}

// ── The contradictions inbox ───────────────────────────────────────

function inbox(
	pairs: MemoryContradiction[],
	route: Route,
	now: number,
): TemplateResult {
	return html`
    <section class="inbox">
      <h2>Contradictions <span class="count">${pairs.length}</span></h2>
      ${
				pairs.length === 0
					? html`<p class="empty">
              No unresolved contradictions in
              ${route.repo ? repoLabel(route.repo) : "any repo"}.
            </p>`
					: html`<ul class="pairs">
              ${pairs.map((pair) => inboxPair(pair, route, now))}
            </ul>`
			}
    </section>
  `;
}

function inboxPair(
	pair: MemoryContradiction,
	route: Route,
	now: number,
): TemplateResult {
	return html`
    <li class="pair">
      <p class="pair-head">
        ${edgeMark(pair.edge, now)}
      </p>
      <div class="sides">
        ${side(pair.a, route, now)} ${side(pair.b, route, now)}
      </div>
      <details class="resolve">
        <summary>Resolve</summary>
        <p>
          Supersede whichever side is wrong, or store a corrected memory and
          supersede both. Once either side is superseded the pair leaves this
          list.
        </p>
        <pre><code>${supersedeCall(pair.a.slug, pair.b.slug)}
${supersedeCall(pair.b.slug, pair.a.slug)}</code></pre>
      </details>
    </li>
  `;
}

/**
 * The call that resolves a pair by keeping `keep`.
 *
 * Shown rather than performed: the UI's read layer is bound to read tools
 * (ADR 0011), and superseding asks for a confirmation this page cannot give.
 */
function supersedeCall(keep: string, drop: string): string {
	return `memory_link(from_slug="${keep}", to_slug="${drop}", kind="supersedes")`;
}

function side(
	endpoint: MemoryContradiction["a"],
	route: Route,
	now: number,
): TemplateResult {
	return html`
    <article class="side">
      <p class="crumbs">
        <span class="badge">${KIND_LABELS[endpoint.kind]}</span>
        <span title=${endpoint.repo ?? ""}>${repoLabel(endpoint.repo)}</span>
      </p>
      <h3>
        <a href=${routeHref(route, { slug: endpoint.slug })}>${endpoint.title}</a>
      </h3>
      <p class="muted">
        ${endpoint.author}
        <span title=${absolute(endpoint.updated_at)}
          >updated ${ago(endpoint.updated_at, now)}</span
        >
        ${confidence(endpoint.confidence)}
      </p>
      <div class="prose">${markdown(endpoint.content)}</div>
    </article>
  `;
}

/** A memory's own confidence score, which is optional and usually unset. */
function confidence(
	value: number | null | undefined,
): TemplateResult | typeof nothing {
	if (value === null || value === undefined) {
		return nothing;
	}
	return html`<span title="The memory's own confidence score"
    >confidence ${value}</span
  >`;
}

// ── Search results ─────────────────────────────────────────────────

function recallPairs(
	pairs: Contradiction[],
	memories: Memory[],
	route: Route,
): TemplateResult | typeof nothing {
	if (pairs.length === 0) {
		return nothing;
	}
	const titles = new Map(memories.map((m) => [m.slug, m.title]));
	const link = (slug: string) =>
		html`<a href=${routeHref(route, { slug })}>${titles.get(slug) ?? slug}</a>`;
	return html`
    <section class="recall-pairs">
      <h2>Contradictions in these results <span class="count">${pairs.length}</span></h2>
      <ul>
        ${pairs.map((pair) => html`<li>${link(pair.a)} contradicts ${link(pair.b)}</li>`)}
      </ul>
    </section>
  `;
}

/**
 * `recall`'s result as its widget draws it (spec §7.1): the contradiction
 * pairs first, then the ranked memories with each contradicted one marked.
 *
 * The same two pieces the memory view's recall mode draws, minus the toolbar,
 * which only makes sense on a page that can issue another read.
 */
export function recallResults(
	result: RecallResult,
	route: Route,
	now = Date.now(),
): TemplateResult {
	const page: MemoryPage = {
		mode: "recall",
		memories: result.memories,
		pairs: result.contradictions,
	};
	return html`
    <div class="memory-view">
      ${recallPairs(page.pairs, page.memories, route)}
      ${memoryTable(page, page.memories, route, now, new Map(), false)}
    </div>
  `;
}

function topicHeading(topic: Topic | null, route: Route): TemplateResult {
	const clear = html`<a href=${routeHref(route, { topic: null })}
    >all memories</a
  >`;
	if (topic === null) {
		return html`<p class="empty">
      No topic <code>${route.topic}</code> in this graph. Back to ${clear}.
    </p>`;
	}
	return html`
    <header class="topic-heading">
      <h2>${topic.name} <span class="badge">${topic.kind}</span></h2>
      <p class="muted">
        Every memory tagged with this topic, across all repos. Back to ${clear}.
      </p>
    </header>
  `;
}

function memoryTable(
	page: MemoryPage,
	shown: Memory[],
	route: Route,
	now: number,
	flagged: Map<string, StaleMemory>,
	checked: boolean,
): TemplateResult {
	if (shown.length === 0) {
		return html`<p class="empty">${emptyMessage(page, route, checked)}</p>`;
	}
	const contradicted = new Set(
		page.mode === "recall"
			? page.pairs.flatMap((pair) => [pair.a, pair.b])
			: [],
	);
	// A topic page keeps the columns it had when its read was `topic_get`,
	// whose slim rows had no author and no tags (a column of dashes would have
	// read as "nobody wrote these"). `recall` sends both now, so showing them
	// is open as a follow-up.
	const full = page.mode !== "topic";
	return html`
    <section>
      <h2>${tableHeading(page)} <span class="count">${shown.length}</span></h2>
      <table class="rows memories">
        <thead>
          <tr>
            <th>Kind</th>
            <th>Title</th>
            <th>Repo</th>
            ${full ? html`<th>Author</th>` : nothing}
            <th>Updated</th>
            ${full ? html`<th>Tags</th>` : nothing}
          </tr>
        </thead>
        <tbody>
          ${shown.map(
						(memory) => html`
              <tr>
                <td><span class="badge">${KIND_LABELS[memory.kind]}</span></td>
                <td>
                  <a href=${routeHref(route, { slug: memory.slug })}
                    >${memory.title}</a
                  >
                  ${
										contradicted.has(memory.slug)
											? html`<span class="badge contradicted">contradicted</span>`
											: nothing
									}
                  ${staleMark(flagged.get(memory.slug))}
                </td>
                <td title=${memory.repo ?? ""}>${repoLabel(memory.repo)}</td>
                ${full ? html`<td>${memory.author ?? "—"}</td>` : nothing}
                <td title=${absolute(memory.updated_at ?? memory.created_at)}>
                  ${ago(memory.updated_at ?? memory.created_at, now)}
                </td>
                ${full ? html`<td>${memory.tags?.join(", ") ?? ""}</td>` : nothing}
              </tr>
            `,
					)}
        </tbody>
      </table>
    </section>
  `;
}

function staleMark(
	entry: StaleMemory | undefined,
): TemplateResult | typeof nothing {
	if (!entry || !hasMissingRef(entry)) {
		return nothing;
	}
	return html`<span class="badge stale-refs" title=${staleTitle(entry)}
    >stale refs</span
  >`;
}

function tableHeading(page: MemoryPage): string {
	switch (page.mode) {
		case "browse":
			return "Memories";
		case "recall":
			return "Recalled";
		case "plain":
			return "Search results";
		case "topic":
			return "Tagged";
	}
}

function emptyMessage(
	page: MemoryPage,
	route: Route,
	checked: boolean,
): string {
	if (route.stale && !checked) {
		return "Nothing to show: symbol refs have not been checked.";
	}
	if (route.stale && page.memories.length > 0) {
		return "No memories here cite a symbol the code graph no longer has.";
	}
	if (page.memories.length > 0) {
		return "No memories match these filters.";
	}
	if (page.mode === "browse") {
		return `No memories in ${route.repo ? repoLabel(route.repo) : "this graph"}.`;
	}
	if (page.mode === "topic") {
		return "No memories are tagged with this topic.";
	}
	return `Nothing matches “${route.q}”.`;
}

// ── The panel ──────────────────────────────────────────────────────

/**
 * One memory and its neighbourhood (spec §6.7).
 *
 * ★ EVERY SECTION IS CONDITIONAL, as in the task panel: a memory with no
 * edges, no topics and no symbol refs is the common case and has to render as
 * a memory, not as six empty headings.
 */
export function memoryDetail(
	panel: MemoryPanel & { memory: Memory },
	route: Route,
	stale: StaleMemory | undefined = undefined,
	now = Date.now(),
): TemplateResult {
	const { memory, neighbors } = panel;
	const checked = new Map(stale?.refs.map((ref) => [ref.symbol_ref, ref]));
	const replacedBy = neighbors.neighbors.superseded_by ?? [];
	return html`
    <div class="memory-detail">
      <p class="crumbs">
        <code>${memory.slug}</code>
        <span class="badge">${KIND_LABELS[memory.kind]}</span>
        ${
					memory.severity
						? html`<span class="badge severity-${memory.severity}"
                >${memory.severity}</span
              >`
						: nothing
				}
        ${
					replacedBy.length
						? html`<span class="badge superseded">superseded</span>`
						: nothing
				}
        ${staleMark(stale)}
      </p>

      ${
				replacedBy.length
					? html`<p class="superseded-note">
              Superseded: read
              ${replacedBy.map(
								(n, index) => html`${index > 0 ? ", " : ""}
                  <a href=${routeHref(route, { slug: n.slug })}>${n.title}</a>`,
							)}
              instead.
            </p>`
					: nothing
			}

      <dl class="facts">
        <dt>Repo</dt>
        <dd title=${memory.repo ?? ""}>${repoLabel(memory.repo)}</dd>
        ${fact("Author", memory.author)}
        <dt>Created</dt>
        <dd title=${absolute(memory.created_at)}>${ago(memory.created_at, now)}</dd>
        ${
					memory.updated_at
						? html`<dt>Updated</dt>
              <dd title=${absolute(memory.updated_at)}>
                ${ago(memory.updated_at, now)}
              </dd>`
						: nothing
				}
        ${fact("Confidence", memory.confidence?.toString())}
        ${fact("Category", memory.category)}
        ${fact("Language", memory.language)}
        ${fact("Tags", memory.tags?.join(", "))}
      </dl>

      ${
				memory.content
					? html`<section class="prose">${markdown(memory.content)}</section>`
					: nothing
			}
      ${topics(memory, route, now)}
      ${NEIGHBOR_KINDS.map((kind) =>
				neighborGroup(kind, neighbors.neighbors[kind] ?? [], route, now),
			)}
      ${
				memory.symbol_refs?.length
					? html`<section class="edges">
              <h3>Symbols</h3>
              <ul class="symbols">
                ${memory.symbol_refs.map((ref) => symbolRef(ref, checked.get(ref)))}
              </ul>
            </section>`
					: nothing
			}
    </div>
  `;
}

/**
 * One symbol ref, with what the stale check said about it when it said
 * anything. A live ref, or one the check never covered, is the bare id.
 */
function symbolRef(
	ref: string,
	resolved: ResolvedSymbolRef | undefined,
): TemplateResult {
	if (resolved?.status === "missing") {
		return html`<li class="ref-missing">
      <code>${ref}</code>
      <span class="badge stale-refs">missing</span>
      ${
				resolved.candidates?.length
					? html`<p class="muted">
              Same name in the code graph:
              ${resolved.candidates.map(
								(candidate, index) =>
									html`${index > 0 ? ", " : ""}<code>${candidate}</code>`,
							)}
            </p>`
					: html`<p class="muted">Nothing by that name in the code graph.</p>`
			}
    </li>`;
	}
	if (resolved?.status === "unknown") {
		return html`<li class="ref-unknown">
      <code>${ref}</code>
      <span class="badge unchecked" title=${resolved.reason ?? ""}
        >unchecked</span
      >
      ${resolved.reason ? html`<p class="muted">${resolved.reason}</p>` : nothing}
    </li>`;
	}
	return html`<li><code>${ref}</code></li>`;
}

function fact(
	label: string,
	value: string | null | undefined,
): TemplateResult | typeof nothing {
	if (!value) {
		return nothing;
	}
	return html`<dt>${label}</dt>
    <dd>${value}</dd>`;
}

function topics(
	memory: Memory,
	route: Route,
	now: number,
): TemplateResult | typeof nothing {
	if (!memory.topics?.length) {
		return nothing;
	}
	return html`
    <section class="edges">
      <h3>Topics</h3>
      <ul class="neighbors">
        ${memory.topics.map(
					(topic) => html`
            <li class=${edgeClass(topic.edge)}>
              <a
                href=${routeHref(route, {
									view: "memory",
									topic: topic.slug,
									q: "",
									slug: null,
									// Facets reset on entering a topic, as they did when its rows
									// were `topic_get`'s slim ones with none of the facet fields.
									facets: DEFAULT_ROUTE.facets,
								})}
                >${topic.name}</a
              >
              <span class="badge">${topic.kind}</span>
              ${edgeMark(topic.edge, now)}
            </li>
          `,
				)}
      </ul>
    </section>
  `;
}

function neighborGroup(
	kind: NeighborKind,
	rows: MemoryNeighbor[],
	route: Route,
	now: number,
): TemplateResult | typeof nothing {
	if (rows.length === 0) {
		return nothing;
	}
	return html`
    <section class="edges">
      <h3>${NEIGHBOR_LABELS[kind]} <span class="count">${rows.length}</span></h3>
      <ul class="neighbors">
        ${rows.map(
					(row) => html`
            <li class=${edgeClass(row.edge)}>
              <a href=${routeHref(route, { slug: row.slug })}>${row.title}</a>
              <span class="badge">${KIND_LABELS[row.kind]}</span>
              <span class="muted" title=${row.repo ?? ""}
                >${repoLabel(row.repo)}</span
              >
              ${edgeMark(row.edge, now)}
            </li>
          `,
				)}
      </ul>
    </section>
  `;
}

function edgeClass(edge: EdgeMeta): string {
	return `edge edge-${edge.confidence ?? "unrecorded"}`;
}

const CONFIDENCE_TITLES = {
	asserted: "Asserted: someone named both ends of this link",
	inferred:
		"Inferred: witan derived this link (e.g. a topic promoted from a free-string tag), and recall weights it less",
	unrecorded:
		"Written before links recorded their provenance; recall scores it as asserted",
};

/**
 * The link's provenance: confidence always, then role, author and when.
 *
 * ★ CONFIDENCE IS ON EVERY EDGE, INCLUDING THE UNSTAMPED ONES. `recall`
 * weights asserted and inferred links differently, and a topic promoted from a
 * free-string tag is a much weaker claim than a link someone named, so the
 * two must never look alike. A `null` confidence is shown as "unrecorded"
 * rather than omitted: omitting it would read as asserted, which is how recall
 * happens to score it but is not what anyone said.
 */
export function edgeMark(edge: EdgeMeta, now = Date.now()): TemplateResult {
	const confidenceLabel = edge.confidence ?? "unrecorded";
	return html`
    <span class="edge-meta">
      <span
        class="badge confidence-${confidenceLabel}"
        title=${CONFIDENCE_TITLES[confidenceLabel]}
        >${confidenceLabel}</span
      >
      ${edge.role ? html`<span class="role">“${edge.role}”</span>` : nothing}
      ${edge.author ? html`<span class="muted">${edge.author}</span>` : nothing}
      ${
				edge.created_at
					? html`<span class="muted" title=${absolute(edge.created_at)}
              >${ago(edge.created_at, now)}</span
            >`
					: nothing
			}
    </span>
  `;
}

/** What the panel shows for a memory slug the graph does not have. */
export function memoryMissing(slug: string): TemplateResult {
	return html`
    <p class="empty">
      No memory <code>${slug}</code> in this graph. The link may be stale, or
      point at another target.
    </p>
  `;
}
