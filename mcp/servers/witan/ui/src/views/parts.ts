import { html, nothing, svg, type TemplateResult } from "lit-html";
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

/** How long the copied tick shows before the button reverts to its icon. */
const COPIED_MS = 1500;

const copyIcon = svg`
  <svg class="icon-copy" viewBox="0 0 16 16" aria-hidden="true">
    <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
    <path d="M10.5 3.5v-.5a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 3v5A1.5 1.5 0 0 0 4 9.5h.5" />
  </svg>
`;

const copiedIcon = svg`
  <svg class="icon-copied" viewBox="0 0 16 16" aria-hidden="true">
    <path d="M3 8.5l3 3 7-7" />
  </svg>
`;

const failedIcon = svg`
  <svg class="icon-failed" viewBox="0 0 16 16" aria-hidden="true">
    <path d="M4 4l8 8M12 4l-8 8" />
  </svg>
`;

/**
 * A button that puts `value` on the clipboard, for pasting a slug into the CLI.
 *
 * The copied state is set on the button's own node rather than held in view
 * state: lit-html leaves an attribute it does not bind alone, so a re-render
 * from a poll in the next 1.5s does not cut the tick short, and nothing outside
 * the button has to know a copy happened. The outcome is a shape as well as a
 * colour (a tick or a cross), and is announced through the button's own live
 * region, since the icons are hidden from assistive tech.
 */
export function copyButton(value: string, label = value): TemplateResult {
	return html`<button
    type="button"
    class="copy"
    title=${`Copy ${label}`}
    aria-label=${`Copy ${label}`}
    @click=${(event: MouseEvent) => {
			// A copy button inside a link or a clickable row must not also
			// navigate it.
			event.preventDefault();
			event.stopPropagation();
			const button = event.currentTarget as HTMLButtonElement;
			const status = button.querySelector(".copy-status");
			void copyText(value).then((copied) => {
				button.dataset.copied = copied ? "yes" : "no";
				if (status) {
					status.textContent = copied ? "Copied" : "Copy failed";
				}
				setTimeout(() => {
					delete button.dataset.copied;
					if (status) {
						status.textContent = "";
					}
				}, COPIED_MS);
			});
		}}
  >${copyIcon}${copiedIcon}${failedIcon}<span
      class="copy-status"
      role="status"
    ></span></button>`;
}

/**
 * Write `text` to the clipboard, saying whether it worked.
 *
 * The async Clipboard API is absent outside a secure context and refused in an
 * MCP Apps iframe the host did not grant `clipboard-write`, so it falls back to
 * the selection-and-`copy` route, which a sandboxed frame usually still allows
 * from inside a click.
 */
export async function copyText(text: string): Promise<boolean> {
	try {
		await navigator.clipboard.writeText(text);
		return true;
	} catch {
		const area = document.createElement("textarea");
		area.value = text;
		area.setAttribute("readonly", "");
		area.style.position = "fixed";
		area.style.opacity = "0";
		document.body.append(area);
		area.select();
		try {
			return document.execCommand("copy");
		} catch {
			return false;
		} finally {
			area.remove();
		}
	}
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
