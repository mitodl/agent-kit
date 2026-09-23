import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const createOidc = vi.fn();
const oidcEarlyInit = vi.fn(() => ({ shouldLoadApp: true }));

vi.mock("oidc-spa/core", () => ({ createOidc }));
vi.mock("oidc-spa/entrypoint", () => ({ oidcEarlyInit }));

const { bearerAuth, earlyInit, LoginRejectedError } = await import("./auth.js");

const DEPLOYED = {
	issuer: "https://sso.example.org/realms/ol-platform-engineering",
	client_id: "witan-ui",
	audience: "witan",
};

function loggedIn(accessToken = "at-1") {
	return {
		isUserLoggedIn: true,
		getTokens: vi.fn(async () => ({ accessToken })),
		goToAuthServer: vi.fn(async () => {}),
	};
}

beforeEach(() => {
	sessionStorage.clear();
	createOidc.mockReset();
	oidcEarlyInit.mockClear();
});

afterEach(() => {
	vi.useRealTimers();
});

describe("bearerAuth", () => {
	it("does nothing at all for a local page", async () => {
		// `witan ui` on 127.0.0.1: no issuer, no login, no credential. Creating
		// an OIDC client here would send the user to a Keycloak that does not
		// exist.
		await expect(bearerAuth(null, "/ui/")).resolves.toBeUndefined();
		expect(createOidc).not.toHaveBeenCalled();
	});

	it("logs in with the static client, by full page redirect", async () => {
		createOidc.mockResolvedValue(loggedIn());

		await bearerAuth(DEPLOYED, "/ui/");

		expect(createOidc).toHaveBeenCalledWith({
			issuerUri: DEPLOYED.issuer,
			clientId: "witan-ui",
			BASE_URL: "/ui/",
			autoLogin: true,
			// The iframe cannot work under the page's CSP; see auth.ts.
			sessionRestorationMethod: "full page redirect",
		});
	});

	it("reads the token per request, so a refreshed one is picked up", async () => {
		const oidc = loggedIn();
		createOidc.mockResolvedValue(oidc);
		const auth = await bearerAuth(DEPLOYED, "/ui/");

		oidc.getTokens.mockResolvedValueOnce({ accessToken: "at-2" });

		await expect(auth?.token()).resolves.toBe("at-2");
		await expect(auth?.token()).resolves.toBe("at-1");
		expect(oidc.getTokens).toHaveBeenCalledTimes(2);
	});

	it("restarts the login on a 401", async () => {
		const oidc = loggedIn();
		createOidc.mockResolvedValue(oidc);
		const auth = await bearerAuth(DEPLOYED, "/ui/");

		await auth?.onUnauthorized();

		expect(oidc.goToAuthServer).toHaveBeenCalledWith({
			redirectUrl: window.location.href,
		});
	});

	it("stops instead of looping when the fresh token is rejected too", async () => {
		// The page reloads between the two 401s (the login is a navigation), so
		// the marker has to outlive the module: two separate instances here.
		vi.useFakeTimers({ now: 1_000_000, toFake: ["Date"] });
		const first = loggedIn();
		createOidc.mockResolvedValue(first);
		await (await bearerAuth(DEPLOYED, "/ui/"))?.onUnauthorized();

		vi.setSystemTime(1_000_000 + 5_000);
		const second = loggedIn();
		createOidc.mockResolvedValue(second);
		const auth = await bearerAuth(DEPLOYED, "/ui/");

		await expect(auth?.onUnauthorized()).rejects.toBeInstanceOf(
			LoginRejectedError,
		);
		expect(second.goToAuthServer).not.toHaveBeenCalled();
	});

	it("restarts again once the loop window has passed", async () => {
		vi.useFakeTimers({ now: 1_000_000, toFake: ["Date"] });
		createOidc.mockResolvedValue(loggedIn());
		await (await bearerAuth(DEPLOYED, "/ui/"))?.onUnauthorized();

		// An hour later the session genuinely expired; that is a login, not a loop.
		vi.setSystemTime(1_000_000 + 3_600_000);
		const later = loggedIn();
		createOidc.mockResolvedValue(later);
		await (await bearerAuth(DEPLOYED, "/ui/"))?.onUnauthorized();

		expect(later.goToAuthServer).toHaveBeenCalledOnce();
	});

	it("refuses an issuer with no client id", async () => {
		await expect(
			bearerAuth({ ...DEPLOYED, client_id: null }, "/ui/"),
		).rejects.toThrow(/WITAN_UI_OIDC_CLIENT_ID/);
		expect(createOidc).not.toHaveBeenCalled();
	});
});

describe("earlyInit", () => {
	it("passes the mount as the base and the redirect mode", () => {
		expect(earlyInit("/witan/ui/")).toBe(true);
		expect(oidcEarlyInit).toHaveBeenCalledWith({
			BASE_URL: "/witan/ui/",
			sessionRestorationMethod: "full page redirect",
		});
	});
});
