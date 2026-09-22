import { html, type nothing, type TemplateResult } from "lit-html";
import { repoLabel } from "./format.js";
import type { Route } from "./route.js";
import { routeHref } from "./route.js";

// `Route` is imported as a type only, so `verbatimModuleSyntax` erases it and
// the route↔shell pair is not a runtime cycle: route.ts needs `VIEWS` to
// validate a fragment, and the shell needs a route to write its links.

/**
 * The tabs the shell carries, in the order spec §6 lists them.
 *
 * Declared here rather than beside each view so the shell has one place to
 * grow from, and so a view landing later is a route plus a render function
 * rather than a second navigation model invented alongside the first.
 */
export const VIEWS = [
	{ id: "projects", label: "Projects" },
	{ id: "board", label: "Board" },
	{ id: "waves", label: "Waves" },
	{ id: "timeline", label: "Timeline" },
	{ id: "memory", label: "Memory" },
	{ id: "graph", label: "Graph" },
] as const;

export type ViewId = (typeof VIEWS)[number]["id"];

/** One option in the repo filter. `slug` is the repo URI; `""` is all repos. */
export interface FilterOption {
	value: string;
	label: string;
}

export interface ShellProps {
	route: Route;
	/**
	 * Repo URIs to offer in the filter, derived from what the reads came back
	 * with rather than configured. A deployed witan has no checkout to
	 * enumerate repos from, and the graph is the only thing that knows which
	 * ones have work in them.
	 */
	repos: string[];
	/** Projects to offer in the project filter, from the current repo scope. */
	projects: FilterOption[];
	/** The read-state line: last read time, staleness, refresh. */
	status: TemplateResult;
	/** The active view. */
	body: TemplateResult;
	/** The detail panel, when a slug is open. */
	panel: TemplateResult | typeof nothing;
	/** Called when a filter changes, with the route patch it implies. */
	onNavigate: (patch: Partial<Route>) => void;
}

/**
 * The app frame: filters, tabs, the view, and the detail panel over it.
 *
 * Everything it renders comes in as a prop. That is what keeps it testable
 * without a server: `shell.test.ts` renders the whole frame from literals.
 */
export function shell(props: ShellProps): TemplateResult {
	const { route, onNavigate } = props;
	return html`
    <header class="shell-header">
      <h1>Witan</h1>
      <nav aria-label="Views">
        ${VIEWS.map(
					(view) => html`
            <a
              href=${routeHref(route, { view: view.id })}
              aria-current=${view.id === route.view ? "page" : "false"}
              >${view.label}</a
            >
          `,
				)}
      </nav>
      <div class="filters">
        <label>
          Repo
          <!--
            The selection is \`?selected\` on the options, not \`.value\` on the
            select. lit-html commits an element's own bindings before its
            children, so a \`.value\` here would run against a select with no
            options yet and do nothing on the first render, then be
            dirty-checked out of later ones. Both selects carry the state on
            their options instead, which works in both passes.
          -->
          <select
            @change=${(event: Event) =>
							onNavigate({
								repo: (event.target as HTMLSelectElement).value,
								// A project belongs to a repo scope, so carrying the
								// selection across a repo change would filter the list to a
								// project that is no longer in it and show nothing.
								project: null,
								slug: null,
							})}
          >
            <option value="">All repos</option>
            ${props.repos.map(
							(repo) =>
								html`<option value=${repo} ?selected=${repo === route.repo}>
                  ${repoLabel(repo)}
                </option>`,
						)}
          </select>
        </label>
        <label>
          Project
          <select
            @change=${(event: Event) =>
							onNavigate({
								project: (event.target as HTMLSelectElement).value || null,
								slug: null,
							})}
          >
            <option value="">All projects</option>
            ${props.projects.map(
							(project) =>
								html`<option
                  value=${project.value}
                  ?selected=${project.value === route.project}
                >
                  ${project.label}
                </option>`,
						)}
          </select>
        </label>
        <label class="closed-toggle">
          <input
            type="checkbox"
            .checked=${route.closed}
            @change=${(event: Event) =>
							onNavigate({
								closed: (event.target as HTMLInputElement).checked,
							})}
          />
          Closed
        </label>
      </div>
      ${props.status}
    </header>
    <div class="workspace">
      <main>${props.body}</main>
      ${props.panel}
    </div>
  `;
}

/**
 * The detail panel, beside whatever view is open.
 *
 * A `<dialog>` is deliberately avoided: the native modal traps focus and
 * blanks the page behind it, and spec §6.1 wants the view underneath to stay
 * readable — the point of a linkable panel is comparing the open task against
 * the list it came from.
 *
 * It sits INSIDE the workspace row rather than over the whole page, so the
 * list reflows beside it and the filter bar stays reachable. A fixed overlay
 * covered the repo and project selects and the read-time line, which made
 * "narrow the list while reading a task" impossible without closing the task
 * first.
 *
 * The close link is a route link, not a handler, so the panel closes the same
 * way it opened and the back button works.
 */
export function detailPanel(
	route: Route,
	title: string,
	body: TemplateResult,
): TemplateResult {
	return html`
    <aside
      class="detail-panel"
      role="complementary"
      aria-label=${title}
      tabindex="-1"
    >
      <header>
        <h2>${title}</h2>
        <a class="close" href=${routeHref(route, { slug: null })} title="Close (Esc)"
          >×</a
        >
      </header>
      ${body}
    </aside>
  `;
}
