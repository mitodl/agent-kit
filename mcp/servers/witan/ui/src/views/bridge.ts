import { html, nothing, svg, type TemplateResult } from "lit-html";
import { ref } from "lit-html/directives/ref.js";
import { emptyBox, errorBox } from "../chrome.js";
import { repoLabel } from "../format.js";
import { CONFIDENCE_FLOORS, type Route, routeHref } from "../route.js";
import type {
	ContractKind,
	DepContract,
	DepEdge,
	InterfaceBinding,
	RepoDependencies,
} from "../types.js";
import type { CanvasHost } from "./canvas-host.js";

/**
 * The Bridge tab: which repos are coupled through shared env vars, endpoints,
 * packages and services, and the bindings behind each coupling.
 *
 * Three levels, each a route field: the repo graph (`code_repo_dependencies`),
 * one edge's contracts (already in that result, so no read), and one
 * contract's `InterfaceBinding` rows (`code_interface_providers` and
 * `_consumers`, narrowed to the edge's two repos). Nothing here derives an
 * edge: witan-code's `build_graph` is the one derivation, and the page only
 * filters what it returned.
 *
 * Shown only when the server has witan-code's tools (ADR 0011, 2026-09-23
 * amendment). The app decides that; this module assumes the tools exist.
 */

/** Edge colours by contract kind, as witan-code's `visualize.KIND_COLORS`. */
export const KIND_COLORS: Record<ContractKind, string> = {
	env_var: "#e8a33d",
	endpoint: "#4c9be8",
	package: "#56b870",
	service: "#b86fd1",
};

const KIND_LABELS: Record<ContractKind, string> = {
	env_var: "env var",
	endpoint: "endpoint",
	package: "package",
	service: "service",
};

/** What the canvas draws: the repos, and the edges left after the filters. */
export interface BridgeGraph {
	repos: string[];
	edges: DepEdge[];
}

/** Called with the clicked edge's `edgeKey`. */
export type OnSelectEdge = (edge: string) => void;

export type BridgeCanvasHost = CanvasHost<BridgeGraph, OnSelectEdge>;

/** An edge's route value. Repo URIs never contain `|`. */
export function edgeKey(edge: Pick<DepEdge, "consumer" | "provider">): string {
	return `${edge.consumer}|${edge.provider}`;
}

export function parseEdgeKey(
	value: string,
): { consumer: string; provider: string } | null {
	const [consumer, provider, ...rest] = value.split("|");
	return consumer && provider && rest.length === 0
		? { consumer, provider }
		: null;
}

/** A contract's route value. Split on the FIRST colon: keys can hold more. */
export function contractKey(
	contract: Pick<DepContract, "kind" | "key">,
): string {
	return `${contract.kind}:${contract.key}`;
}

export function parseContractKey(
	value: string,
): { kind: ContractKind; key: string } | null {
	const at = value.indexOf(":");
	const kind = value.slice(0, at);
	const key = value.slice(at + 1);
	return at > 0 && key && Object.hasOwn(KIND_COLORS, kind)
		? { kind: kind as ContractKind, key }
		: null;
}

/**
 * Whether any contract says whether it is generic. A witan-code older than
 * that field sends none, and a toggle that could never hide anything would
 * read as "there are no generic keys".
 */
export function knowsGeneric(deps: RepoDependencies): boolean {
	return deps.edges.some((edge) =>
		edge.contracts.some((contract) => contract.generic !== undefined),
	);
}

/**
 * The edges the route's browser-side filters leave, with each edge's weight
 * and kinds recounted from the contracts that survived.
 *
 * With the defaults (all repos, floor 0.5, generic shown) this is the tool's
 * result unchanged: the server already drops endpoint consumers under 0.5.
 *
 * ★ THE REPO IS MATCHED EXACTLY HERE, on top of the tool's own filter. The
 * tool's `repo` is a substring, so scoping to `.../ol-django` also keeps the
 * edges of `.../ol-django-extras`. The route's repo is a whole canonical URI,
 * and an edge in scope is one with that repo at either end.
 */
export function visibleEdges(deps: RepoDependencies, route: Route): DepEdge[] {
	return deps.edges.flatMap((edge) => {
		if (
			route.repo &&
			edge.consumer !== route.repo &&
			edge.provider !== route.repo
		) {
			return [];
		}
		const contracts = edge.contracts.filter(
			(contract) =>
				contract.confidence >= route.confidence &&
				!(route.hideGeneric && contract.generic === true),
		);
		if (contracts.length === 0) {
			return [];
		}
		const kinds: Record<string, number> = {};
		for (const contract of contracts) {
			kinds[contract.kind] = (kinds[contract.kind] ?? 0) + 1;
		}
		return [{ ...edge, contracts, kinds, weight: contracts.length }];
	});
}

/** Which binding reads open one contract of one edge, and how to narrow them. */
export interface DrillPlan {
	providers: { kind: ContractKind; key: string; repo: string } | null;
	consumers: { kind: ContractKind; key: string; repo: string } | null;
}

/**
 * The reads behind one contract on one edge.
 *
 * An ordinary edge is `consumer` reading what `provider` provides, so it is
 * the provider rows from the provider repo and the consumer rows from the
 * consumer repo. A `service` edge is the other way about: witan-code draws
 * "the deploying repo depends on what it deploys", from the DEPLOYING repo's
 * provider binding keyed `repo:<deployed uri>`, and the contract's `key` is
 * only the deployed repo's short name. So it reads that one binding.
 */
export function drillPlan(
	edge: { consumer: string; provider: string },
	contract: { kind: ContractKind; key: string },
): DrillPlan {
	if (contract.kind === "service") {
		return {
			providers: {
				kind: "service",
				key: `repo:${edge.provider}`,
				repo: edge.consumer,
			},
			consumers: null,
		};
	}
	return {
		providers: { ...contract, repo: edge.provider },
		consumers: { ...contract, repo: edge.consumer },
	};
}

export interface BridgeViewProps {
	deps: RepoDependencies;
	route: Route;
	host: BridgeCanvasHost;
	/** The open contract's bindings, drawn by the app from its own read. */
	drill: TemplateResult | typeof nothing;
	onNavigate: (patch: Partial<Route>) => void;
}

export function bridgeView(props: BridgeViewProps): TemplateResult {
	const { deps, route, host } = props;
	const edges = visibleEdges(deps, route);
	if (host.error) {
		return html`${controls(props)}
      ${errorBox(
				new Error(`${host.error.message} Reload the page to try again.`),
				"the graph drawing code",
			)}`;
	}
	const open = route.edge ? parseEdgeKey(route.edge) : null;
	// Under a repo filter, the repos at the ends of its edges (or the repo
	// alone, if it has none), rather than every repo the substring matched.
	const repos = route.repo
		? [
				...new Set([
					route.repo,
					...edges.flatMap((e) => [e.consumer, e.provider]),
				]),
			]
		: deps.repos;
	host.show({ repos, edges }, route.edge);
	return html`
    <section class="bridge" aria-label="Bridge">
      ${controls(props)}
      <p class="note">
        ${repos.length} ${repos.length === 1 ? "repo" : "repos"} ·
        ${edges.length} ${edges.length === 1 ? "edge" : "edges"}. An edge points
        from the repo that depends to the repo it depends on. Choose one for
        the contracts behind it.
      </p>
      ${
				edges.length === 0
					? emptyBox("No cross-repo edges in the code graph for this scope.")
					: html`${legend()}
            <div
              class="graph-canvas bridge-canvas"
              role="img"
              aria-label="Repo dependency graph. The table below lists every edge as a link."
              ${ref(host.attach)}
            ></div>
            ${edgeTable(edges, route)}`
			}
      ${open ? edgeDetail(open, edges, route, props.drill) : nothing}
    </section>
  `;
}

function controls(props: BridgeViewProps): TemplateResult {
	const { route, onNavigate, deps } = props;
	// The open contract may not survive a filter change, and a drill-down
	// under a filter that hides it would disagree with the list beside it.
	const narrow = (patch: Partial<Route>) =>
		onNavigate({ ...patch, binding: null });
	return html`
    <div class="filters bridge-controls">
      <label>
        Kind
        <select
          @change=${(event: Event) =>
						narrow({
							contract:
								((event.target as HTMLSelectElement).value as ContractKind) ||
								null,
						})}
        >
          <option value="">All kinds</option>
          ${(Object.keys(KIND_LABELS) as ContractKind[]).map(
						(kind) =>
							html`<option value=${kind} ?selected=${kind === route.contract}>
                ${KIND_LABELS[kind]}
              </option>`,
					)}
        </select>
      </label>
      <label
        title="Endpoint consumers carry a trust score that suppresses phantom calls"
      >
        Confidence at least
        <select
          @change=${(event: Event) =>
						narrow({
							confidence: Number((event.target as HTMLSelectElement).value),
						})}
        >
          ${CONFIDENCE_FLOORS.map(
						(floor) =>
							html`<option
                value=${String(floor)}
                ?selected=${floor === route.confidence}
              >
                ${floor}
              </option>`,
					)}
        </select>
      </label>
      ${
				knowsGeneric(deps)
					? html`<label title="Stoplisted keys such as DEBUG and PORT">
              <input
                type="checkbox"
                .checked=${route.hideGeneric}
                @change=${(event: Event) =>
									narrow({
										hideGeneric: (event.target as HTMLInputElement).checked,
									})}
              />
              Hide generic keys
            </label>`
					: nothing
			}
      <label
        title="Only edges a Stage-2 canonical-symbol join also covers"
      >
        <input
          type="checkbox"
          .checked=${route.precise}
          @change=${(event: Event) =>
						narrow({ precise: (event.target as HTMLInputElement).checked })}
        />
        Precise only
      </label>
    </div>
  `;
}

/**
 * Every edge as a link, the same selection the canvas makes.
 *
 * The canvas is pointer-only, so this is the way to the drill-down for a
 * keyboard or a screen reader, and the one place an edge's weight and kinds
 * are readable as text.
 */
function edgeTable(edges: DepEdge[], route: Route): TemplateResult {
	const sorted = [...edges].sort((a, b) => b.weight - a.weight);
	return html`
    <table class="rows bridge-edges">
      <thead>
        <tr>
          <th scope="col">Depends</th>
          <th scope="col">On</th>
          <th scope="col">Contracts</th>
          <th scope="col">Kinds</th>
        </tr>
      </thead>
      <tbody>
        ${sorted.map((edge) => {
					const key = edgeKey(edge);
					return html`<tr aria-current=${key === route.edge ? "true" : "false"}>
            <td>
              <a href=${routeHref(route, { edge: key, binding: null })}
                >${repoLabel(edge.consumer)}</a
              >
            </td>
            <td>${repoLabel(edge.provider)}</td>
            <td>${edge.weight}</td>
            <td>
              ${Object.entries(edge.kinds)
								.map(
									([kind, count]) =>
										`${KIND_LABELS[kind as ContractKind] ?? kind} ${count}`,
								)
								.join(", ")}
            </td>
          </tr>`;
				})}
      </tbody>
    </table>
  `;
}

function edgeDetail(
	open: { consumer: string; provider: string },
	edges: DepEdge[],
	route: Route,
	drill: TemplateResult | typeof nothing,
): TemplateResult {
	const edge = edges.find((candidate) => edgeKey(candidate) === route.edge);
	const heading = html`<h2 class="bridge-edge-heading">
    ${repoLabel(open.consumer)} depends on ${repoLabel(open.provider)}
  </h2>`;
	if (!edge) {
		return html`<section class="bridge-edge">
      ${heading}
      <p class="note">
        No contract on this edge passes the current filters, or the edge is
        not in this scope.
        <a href=${routeHref(route, { edge: null, binding: null })}>Close it</a>.
      </p>
    </section>`;
	}
	return html`
    <section class="bridge-edge" aria-label="Edge">
      ${heading}
      <ul class="bridge-contracts">
        ${edge.contracts.map((contract) => {
					const key = contractKey(contract);
					return html`<li aria-current=${key === route.binding ? "true" : "false"}>
            <span class="bridge-kind">${KIND_LABELS[contract.kind]}</span>
            <a href=${routeHref(route, { binding: key })}
              ><code>${contract.key}</code></a
            >
            ${
							contract.kind === "service"
								? html`<span class="muted">(deploys it)</span>`
								: nothing
						}
            ${
							contract.confidence < 1
								? html`<span class="muted"
                    >confidence ${contract.confidence}</span
                  >`
								: nothing
						}
            ${contract.generic ? html`<span class="muted">generic</span>` : nothing}
          </li>`;
				})}
      </ul>
      ${drill}
    </section>
  `;
}

/**
 * The Stage-2 canonical symbol's package half, when it resolved that far.
 *
 * `{scheme}:{manager}:{package}:{version}:{descriptor}`, with `.` for a field
 * that did not resolve. "These two repos share DATABASE_URL" and "this repo
 * provides the package that one imports" are different claims, and this is
 * the part that says which.
 */
export function stage2(symbol: string | null): string | null {
	if (!symbol) {
		return null;
	}
	const [, manager, pkg, version] = symbol.split(":");
	const known = [manager, pkg, version].filter(
		(field): field is string => !!field && field !== ".",
	);
	return known.length > 0 ? known.join(" ") : null;
}

/**
 * The rows that could have made the edge at this floor.
 *
 * `code_repo_dependencies` builds its edges only from endpoint consumers at
 * or above 0.5, and the page's floor can raise that; the binding tools apply
 * no floor at all. So an endpoint consumer row under the floor did not produce
 * the edge being drilled, and is left out. A row with no score reads as 1, as
 * witan-code's own readers treat it.
 */
export function producingRows(
	rows: InterfaceBinding[],
	floor: number,
): InterfaceBinding[] {
	return rows.filter(
		(row) =>
			row.role !== "consumer" ||
			row.kind !== "endpoint" ||
			(row.confidence ?? 1) >= floor,
	);
}

/** One contract's bindings on the open edge. */
export function bindingTable(rows: InterfaceBinding[]): TemplateResult {
	if (rows.length === 0) {
		return emptyBox(
			"No bindings for this contract in these two repos. The bridge may have been reindexed since the graph was read.",
		);
	}
	return html`
    <table class="rows bridge-bindings">
      <thead>
        <tr>
          <th scope="col">Role</th>
          <th scope="col">Repo</th>
          <th scope="col">As written</th>
          <th scope="col">Where</th>
          <th scope="col">Framework</th>
          <th scope="col">Symbol</th>
          <th scope="col" title="The Stage-2 canonical symbol's package and version, where it resolved">Package (Stage 2)</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map(
					(row) => html`<tr>
            <td>
              ${row.role}
              ${
								row.confidence != null && row.confidence < 1
									? html`<span class="muted">${row.confidence.toFixed(2)}</span>`
									: nothing
							}
            </td>
            <td>${repoLabel(row.repo)}</td>
            <td><code>${row.key}</code></td>
            <td>
              <code>${row.file}${row.line === null ? "" : `:${row.line}`}</code>
            </td>
            <td>
              ${[row.framework, row.language].filter(Boolean).join(" · ")}
            </td>
            <td>${row.symbol_id ? html`<code>${row.symbol_id}</code>` : nothing}</td>
            <td>${stage2(row.symbol) ?? nothing}</td>
          </tr>`,
				)}
      </tbody>
    </table>
  `;
}

/** The key: one swatch per contract kind, as SVG for the CSP (see graph.ts). */
function legend(): TemplateResult {
	return html`
    <ul class="gr-legend" aria-label="Key">
      ${(Object.keys(KIND_COLORS) as ContractKind[]).map(
				(kind) => html`<li>
          <svg class="gr-swatch" viewBox="0 0 10 10" aria-hidden="true">
            ${svg`<line x1="0" y1="5" x2="10" y2="5" stroke=${KIND_COLORS[kind]} stroke-width="2"></line>`}
          </svg>
          ${KIND_LABELS[kind]}
        </li>`,
			)}
    </ul>
  `;
}
