import { html, nothing, type TemplateResult } from "lit-html";
import { isLinkable } from "../format.js";
import { type Route, routeHref } from "../route.js";

/**
 * Template fragments more than one view draws.
 *
 * Extracted on the second use, not the first: the projects view and the task
 * detail pane both render a comma-joined list of slugs as links, and both
 * render a fact row that is an anchor when its value is a URL and text when it
 * is not.
 */

/**
 * A comma-joined list of slugs, each a link to `patch(slug)`.
 *
 * The patch is a function rather than a route key because the two callers
 * navigate differently with the same markup: a task's blockers open in the
 * detail panel, a project's open the rollup.
 */
export function slugLinks(
	label: string,
	slugs: string[],
	route: Route,
	patch: (slug: string) => Partial<Route>,
): TemplateResult | typeof nothing {
	if (slugs.length === 0) {
		return nothing;
	}
	return html`
    <p>
      <strong>${label}</strong>:
      ${slugs.map(
				(slug, index) => html`${index > 0 ? ", " : ""}
          <a href=${routeHref(route, patch(slug))}><code>${slug}</code></a>`,
			)}
    </p>
  `;
}

/**
 * A `<dt>/<dd>` pair whose value is a link when it is one, and text when it is not.
 *
 * `external_uri` and the GitHub fields are free text: they hold bare ids and
 * notes as often as URLs, and `isLinkable` is what stops a `javascript:` value
 * becoming an `href`.
 */
export function uriFact(
	label: string,
	uri: string | null | undefined,
): TemplateResult | typeof nothing {
	if (!uri) {
		return nothing;
	}
	return html`
    <dt>${label}</dt>
    <dd>${isLinkable(uri) ? html`<a href=${uri}>${uri}</a>` : uri}</dd>
  `;
}
