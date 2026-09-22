import { html, nothing, type TemplateResult } from "lit-html";
import { absolute, ago } from "./format.js";
import { hasResult, isStale, type Snapshot } from "./live.js";

/**
 * The loading, empty, error and stale states, in one place.
 *
 * Every view needs all four and they are the states most easily got wrong: an
 * empty result rendered as an error, or a failed refresh rendered as an empty
 * graph. Discovery found the CLI confusing exactly those two, so the UI keeps
 * them apart in a single module the views cannot route around.
 */

/**
 * The read-state line in the top bar: when the data was read, and how it is doing.
 *
 * Shows the read time always (spec §6.1), a stale marker when the last refresh
 * failed, and a refresh button that works in every state — including a failed
 * first read, which is the one moment someone most wants to retry.
 */
export function readStatus(
	snapshot: Snapshot<unknown>,
	refresh: () => void,
	now = Date.now(),
): TemplateResult {
	return html`
    <div class="read-status" role="status">
      ${
				isStale(snapshot)
					? html`<span class="stale" title=${snapshot.error?.message ?? ""}
              >stale — last refresh failed</span
            >`
					: nothing
			}
      ${
				snapshot.loadedAt
					? html`<span title=${absolute(new Date(snapshot.loadedAt))}
              >read ${ago(new Date(snapshot.loadedAt), now)}</span
            >`
					: nothing
			}
      <button type="button" @click=${refresh} ?disabled=${snapshot.loading}>
        ${snapshot.loading ? "Reading…" : "Refresh"}
      </button>
    </div>
  `;
}

/**
 * What a view renders instead of its body, or `null` to render the body.
 *
 * ★ STALE DATA IS NOT ONE OF THESE. A snapshot with data and an error still
 * renders its body — the error is reported by `readStatus` and the data stays
 * on screen. Returning a placeholder here on any error is precisely the
 * blanking spec §6.1 rules out.
 */
export function placeholderFor<T>(
	snapshot: Snapshot<T>,
	label: string,
): TemplateResult | null {
	if (hasResult(snapshot)) {
		return null;
	}
	if (snapshot.error) {
		return errorBox(snapshot.error, label);
	}
	return html`<p class="placeholder" aria-busy="true">Reading ${label}…</p>`;
}

/** A read that failed with nothing to fall back on. */
export function errorBox(error: Error, label: string): TemplateResult {
	return html`
    <div class="error-box" role="alert">
      <p>Could not read ${label}.</p>
      <pre>${error.message}</pre>
    </div>
  `;
}

/**
 * A read that succeeded and returned nothing.
 *
 * Separate from `errorBox` and worded as a fact about the graph, because "no
 * projects in this repo" and "the read failed" are different situations and
 * one of them is not a problem.
 */
export function emptyBox(message: string): TemplateResult {
	return html`<p class="empty">${message}</p>`;
}
