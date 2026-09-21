import { html, type TemplateResult } from "lit-html";

/**
 * The tabs the shell will carry, in the order spec §6 lists them.
 *
 * Declared here while every one of them is still unbuilt so the shell has one
 * place to grow from, and so a view landing later is a route plus a render
 * function rather than a second navigation model invented alongside the first.
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

/**
 * The app frame: a tab bar and the slot a view renders into.
 *
 * `body` is passed in rather than resolved here because the views arrive in
 * separate tasks (spec §10 #6, #7, #9, #11, #12, #13) and each needs the read
 * layer, which does not exist yet either. Until one does, the caller passes
 * the placeholder and nothing about this function has to change when it stops
 * doing so.
 */
export function shell(active: ViewId, body: TemplateResult): TemplateResult {
	return html`
    <header class="shell-header">
      <h1>Witan</h1>
      <nav>
        ${VIEWS.map(
					(view) => html`
            <a
              href="#${view.id}"
              aria-current=${view.id === active ? "page" : "false"}
              >${view.label}</a
            >
          `,
				)}
      </nav>
    </header>
    <main>${body}</main>
  `;
}

/** What `main` holds until the first view lands. */
export function placeholder(): TemplateResult {
	return html`<p class="placeholder">No views are built yet.</p>`;
}

/**
 * The view named by the URL fragment, or the first one.
 *
 * An unknown fragment falls back rather than rendering an empty frame: a stale
 * bookmark should land somewhere, and there is no 404 to serve from a hash.
 */
export function viewFromHash(hash: string): ViewId {
	const id = hash.replace(/^#/, "");
	const match = VIEWS.find((view) => view.id === id);
	return match ? match.id : VIEWS[0].id;
}
