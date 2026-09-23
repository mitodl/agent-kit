import { createOidc } from "oidc-spa/core";
import { oidcEarlyInit } from "oidc-spa/entrypoint";

/**
 * The deployed page's login (spec §8). Local `witan ui` never gets here past
 * `earlyInit`: its `/ui/config.json` says `"auth": null` and the page sends no
 * credential at all.
 *
 * oidc-spa rather than the MCP client's `OAuthClientProvider`: Keycloak
 * 26.7.2 has no RFC 8707 resource indicators, and a plain OIDC client needs
 * none. The `witan` audience comes from the Keycloak client's audience mapper,
 * not from anything this page asks for.
 */

/** What `/ui/config.json` says about login. `null` is the local, no-login case. */
export interface AuthConfig {
	issuer: string;
	client_id: string | null;
	audience: string | null;
}

/**
 * The bearer-token hook the streamable-http transport calls. Structurally the
 * MCP client's `AuthProvider`, declared here so this module does not import
 * the MCP package (`mcp.ts` stays the only file that knows it speaks MCP).
 */
export interface BearerAuth {
	token(): Promise<string | undefined>;
	onUnauthorized(): Promise<void>;
}

/**
 * ★ FULL PAGE REDIRECT, NEVER THE IFRAME. oidc-spa's "auto" picks an iframe
 * whenever the page and Keycloak share a parent domain, which
 * `witan.ol.mit.edu` and `sso.ol.mit.edu` do. The iframe cannot work here:
 * the page's CSP (`default-src 'self'`) blocks framing Keycloak, and the
 * `/ui/` responses carry `frame-ancestors 'none'`, so the redirect back into
 * the frame is blocked too. The login would time out instead of failing.
 *
 * With one client and no iframe, oidc-spa keeps the tokens in memory only
 * (it falls back to sessionStorage only when a second non-iframe client
 * exists in the same tab), so a reload re-runs the redirect and Keycloak's
 * SSO session answers it without a prompt.
 */
const SESSION_RESTORATION = "full page redirect";

/**
 * Must run before anything reads or rewrites the URL: when Keycloak redirects
 * back, the code and state are in the fragment, and this takes them out and
 * puts the page's own route back. Harmless on a page with no auth response in
 * its URL, which is every local page.
 *
 * @param base The mount the bundle is served from, e.g. `/ui/`. It is also the
 *   redirect URI Keycloak must list: oidc-spa sends the user back to the base
 *   and restores the original location from its state.
 * @returns Whether the app should start.
 */
export function earlyInit(base: string): boolean {
	return oidcEarlyInit({
		BASE_URL: base,
		sessionRestorationMethod: SESSION_RESTORATION,
	}).shouldLoadApp;
}

/**
 * A 401 right after a fresh login is not something another login fixes.
 *
 * The usual cause is the token's audience: Keycloak issued it, but without
 * the `witan` audience mapper on the client witan rejects it. Restarting the
 * login on every 401 would bounce between the page and Keycloak forever, so a
 * second restart inside this window stops and says why.
 */
const LOGIN_LOOP_WINDOW_MS = 60_000;
const LOGIN_RESTARTED_AT = "witan-ui:login-restarted-at";

/** witan rejected a token Keycloak had just issued. */
export class LoginRejectedError extends Error {
	constructor(issuer: string) {
		super(
			`witan rejected the token ${issuer} issued moments ago. The page will ` +
				"not log in again on its own: check that the witan-ui client has " +
				"the witan audience mapper.",
		);
		this.name = "LoginRejectedError";
	}
}

/**
 * Log in if the config asks for it, and hand back the transport's token hook.
 *
 * Resolves `undefined` when the config has no `auth`, which is `witan ui`
 * against a loopback store. When it does, this may never resolve: with
 * `autoLogin`, oidc-spa navigates to Keycloak and the page unloads.
 */
export async function bearerAuth(
	auth: AuthConfig | null,
	base: string,
): Promise<BearerAuth | undefined> {
	if (auth === null) {
		return undefined;
	}
	if (!auth.client_id) {
		// The server answers 503 before serving a page in this state, so the
		// page reading it here means the two disagree about what they are.
		throw new Error(
			"/ui/config.json names an issuer but no client_id; the server should " +
				"have refused to serve this page without WITAN_UI_OIDC_CLIENT_ID.",
		);
	}

	const oidc = await createOidc({
		issuerUri: auth.issuer,
		clientId: auth.client_id,
		BASE_URL: base,
		autoLogin: true,
		sessionRestorationMethod: SESSION_RESTORATION,
	});

	return {
		// getTokens refreshes with the refresh token when the access token is
		// near expiry, so every request gets a live one without a timer here.
		token: async () => (await oidc.getTokens()).accessToken,
		onUnauthorized: () => {
			// Parallel reads each get their own 401. They share the one restart
			// rather than the second tripping the loop guard the first just set.
			if (restarting) {
				return restarting;
			}
			// ★ ONCE TRIPPED, FOR THE LIFE OF THE PAGE. The views keep polling,
			// and each poll reconnects; a guard that expired with its window
			// would redirect again on the first poll after it, every minute or
			// so, for as long as the tab stayed open.
			const last = Number(sessionStorage.getItem(LOGIN_RESTARTED_AT));
			if (rejected || (last && Date.now() - last < LOGIN_LOOP_WINDOW_MS)) {
				rejected = true;
				return Promise.reject(new LoginRejectedError(auth.issuer));
			}
			sessionStorage.setItem(LOGIN_RESTARTED_AT, String(Date.now()));
			restarting = oidc.goToAuthServer({
				redirectUrl: window.location.href,
			});
			return restarting;
		},
	};
}

/** The restart in flight, if any. Module state, because it spans reconnects. */
let restarting: Promise<never> | null = null;
/** The loop guard tripped; no further 401 on this page redirects. */
let rejected = false;
