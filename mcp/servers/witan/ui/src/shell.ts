import { html, nothing, type TemplateResult } from "lit-html";
import { UNASSIGNED } from "./assignee.js";
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
	// Shown only when the server has the code graph (see `ShellProps`).
	{ id: "bridge", label: "Bridge" },
	// Not a tab: the header's search box is its way in (see `searchBox`).
	{ id: "search", label: "Search" },
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
	/**
	 * The people to offer in the assignee filter, normalized (see
	 * `assignee.ts`). Derived from the tasks the active view has loaded, since
	 * the graph has no list of people to enumerate.
	 */
	assignees: string[];
	/** The read-state line: last read time, staleness, refresh. */
	status: TemplateResult;
	/** The active view. */
	body: TemplateResult;
	/** The detail panel, when a slug is open. */
	panel: TemplateResult | typeof nothing;
	/** Called when a filter changes, with the route patch it implies. */
	onNavigate: (patch: Partial<Route>) => void;
	/**
	 * Called when the Assignee menu opens or closes. On the Projects list the
	 * people to offer come from a task read the page makes only while someone
	 * is looking at them or filtering by them, so it has to know.
	 */
	onAssigneeMenu: (open: boolean) => void;
	/**
	 * Whether the server has witan-code's tools. Without them the Bridge tab
	 * is left out rather than drawn and broken: witan-code is optional.
	 */
	codeGraph: boolean;
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
        ${VIEWS.filter(
					(view) =>
						view.id !== "search" && (view.id !== "bridge" || props.codeGraph),
				).map(
					(view) => html`
            <a
              href=${routeHref(route, { view: view.id, find: "" })}
              aria-current=${view.id === route.view ? "page" : "false"}
              >${view.label}</a
            >
          `,
				)}
      </nav>
      <div class="filters">
        ${searchBox(route, onNavigate)}
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
        ${assigneeFilter(route, props.assignees, onNavigate, props.onAssigneeMenu)}
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
 * The assignee multi-select: a checkbox per person, with Unassigned pinned at
 * the top.
 *
 * A `<details>` rather than a `<select multiple>`, which is unusable by mouse
 * without a modifier key. The open state is the element's own, so a poll
 * re-render, which does not bind `open`, leaves the list open under the cursor.
 * Each change is one navigation, so the selection is in the URL the moment it
 * is made. The Projects list keeps a project when one of its tasks matches;
 * a project has no assignee of its own.
 */
function assigneeFilter(
	route: Route,
	people: string[],
	onNavigate: (patch: Partial<Route>) => void,
	onMenu: (open: boolean) => void,
): TemplateResult {
	const toggle = (value: string, on: boolean) =>
		onNavigate({
			assignees: on
				? [...route.assignees, value]
				: route.assignees.filter((selected) => selected !== value),
			// The open task may not be in the narrowed list.
			slug: null,
		});
	const option = (value: string, label: string) => html`
    <label>
      <input
        type="checkbox"
        .checked=${route.assignees.includes(value)}
        @change=${(event: Event) =>
					toggle(value, (event.target as HTMLInputElement).checked)}
      />
      ${label}
    </label>
  `;
	const count = route.assignees.length;
	return html`
    <details
      class="assignee-filter"
      @toggle=${(event: Event) =>
				onMenu((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>
        Assignee${count > 0 ? html` <span class="count">${count}</span>` : nothing}
      </summary>
      <div class="assignee-options">
        ${option(UNASSIGNED, "Unassigned")}
        ${people.map((person) => option(person, person))}
        ${
					count > 0
						? html`<button
                type="button"
                @click=${() => onNavigate({ assignees: [], slug: null })}
              >
                Clear
              </button>`
						: nothing
				}
      </div>
    </details>
  `;
}

/**
 * The search box over projects and tasks.
 *
 * In the filter bar rather than on the Search view, so it is reachable from
 * every tab and searches within the repo and project already chosen beside
 * it. A tab link clears the text (see the nav above): the other views do not
 * filter on it, and a box still holding a query over an unfiltered board
 * reads as a board that was filtered.
 *
 * On submit rather than per keystroke, as the memory search is: the task half
 * is a tool call, and a navigation per character would put one history entry
 * per character behind the back button.
 */
function searchBox(
	route: Route,
	onNavigate: (patch: Partial<Route>) => void,
): TemplateResult {
	return html`
    <form
      class="search-box"
      role="search"
      @submit=${(event: SubmitEvent) => {
				event.preventDefault();
				const find = new FormData(event.currentTarget as HTMLFormElement).get(
					"find",
				);
				onNavigate({
					view: "search",
					find: typeof find === "string" ? find.trim() : "",
					slug: null,
				});
			}}
    >
      <input
        type="search"
        name="find"
        aria-label="Search projects and tasks"
        placeholder="Search projects and tasks"
        .value=${route.find}
      />
      <button type="submit">Search</button>
    </form>
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
	status: TemplateResult | typeof nothing = nothing,
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
        ${
					/*
					 * The panel's OWN read state, not the top bar's. The bar reports
					 * the view underneath, so a detail poll that failed over a task
					 * still on screen had no stale marker anywhere and Refresh retried
					 * the wrong read — the one case the retain-last-good rule exists
					 * for, silently unreported.
					 */
					status
				}
        <a
          class="close"
          href=${routeHref(route, { slug: null })}
          aria-label="Close details"
          title="Close (Esc)"
          >×</a
        >
      </header>
      ${body}
    </aside>
  `;
}
