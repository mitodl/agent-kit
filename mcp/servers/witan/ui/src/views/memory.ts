import { html, nothing, type TemplateResult } from "lit-html";
import { absolute, ago, repoLabel } from "../format.js";
import {
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

/** What the memory view's main area renders from. One read per route. */
export type MemoryPage =
	| {
			mode: "browse";
			memories: Memory[];
			/** Every pair in scope, from `memory_contradictions`. */
			inbox: InboxPair[];
	  }
	| {
			mode: "recall";
			memories: Memory[];
			/**
			 * `recall`'s own pairs, which are only the ones with BOTH sides in its
			 * result. Shown as recall reports them rather than joined against the
			 * inbox, so this view and an agent calling `recall` see the same thing.
			 */
			pairs: Contradiction[];
	  }
	| { mode: "plain"; memories: Memory[] }
	| {
			mode: "topic";
			/** `null` when no such Topic exists: a stale link, not a failure. */
			topic: Topic | null;
			memories: Memory[];
	  };

/**
 * One pair, with each side's full memory.
 *
 * `memory_contradictions` carries a projection of each endpoint with no
 * `content`, and the inbox is useless without the two bodies, so each side is
 * read with `memory_get`. A side that reads `null` was deleted after the edge
 * was written; the pair still renders from the projection.
 */
export interface InboxPair {
	pair: MemoryContradiction;
	a: Memory | null;
	b: Memory | null;
}

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

// ── The main area ──────────────────────────────────────────────────

export function memoryView(
	page: MemoryPage,
	route: Route,
	onNavigate: (patch: Partial<Route>) => void,
	now = Date.now(),
): TemplateResult {
	const shown = applyFacets(page.memories, route.facets);
	return html`
    <div class="memory-view">
      ${toolbar(route, facetOptions(page.memories), onNavigate)}
      ${page.mode === "browse" ? inbox(page.inbox, route, now) : nothing}
      ${page.mode === "recall" ? recallPairs(page.pairs, page.memories, route) : nothing}
      ${page.mode === "topic" ? topicHeading(page.topic, route) : nothing}
      ${
				page.mode === "topic" && page.topic === null
					? nothing
					: memoryTable(page, shown, route, now)
			}
    </div>
  `;
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
				// A search replaces a topic rather than narrowing it: topic_get takes
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
      ${
				// `topic_get` takes no superseded flag, so in a topic the toggle
				// would be a control that changes nothing.
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

function inbox(pairs: InboxPair[], route: Route, now: number): TemplateResult {
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

function inboxPair(item: InboxPair, route: Route, now: number): TemplateResult {
	const { pair } = item;
	return html`
    <li class="pair">
      <p class="pair-head">
        ${edgeMark(pair.edge, now)}
      </p>
      <div class="sides">
        ${side(pair.a, item.a, route, now)} ${side(pair.b, item.b, route, now)}
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
	memory: Memory | null,
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
        ${confidence(memory?.confidence)}
      </p>
      ${
				memory?.content
					? html`<p class="prose">${memory.content}</p>`
					: memory === null
						? html`<p class="empty">
                This memory no longer exists; only the link does.
              </p>`
						: nothing
			}
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
): TemplateResult {
	if (shown.length === 0) {
		return html`<p class="empty">${emptyMessage(page, route)}</p>`;
	}
	const contradicted = new Set(
		page.mode === "recall"
			? page.pairs.flatMap((pair) => [pair.a, pair.b])
			: [],
	);
	// `topic_get` returns slim rows with no author and no tags, and a column of
	// dashes reads as "nobody wrote these" rather than "not in this read".
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

function emptyMessage(page: MemoryPage, route: Route): string {
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
	now = Date.now(),
): TemplateResult {
	const { memory, neighbors } = panel;
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
					? html`<section class="prose"><p>${memory.content}</p></section>`
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
                ${memory.symbol_refs.map((ref) => html`<li><code>${ref}</code></li>`)}
              </ul>
            </section>`
					: nothing
			}
    </div>
  `;
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
