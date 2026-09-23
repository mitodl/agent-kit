import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const createOidc = vi.fn();
const oidcEarlyInit = vi.fn(() => ({ shouldLoadApp: true }));

vi.mock("oidc-spa/core", () => ({ createOidc }));
vi.mock("oidc-spa/entrypoint", () => ({ oidcEarlyInit }));

/**
 * A fresh copy of the module, which is what a page load is: the restart in
 * flight and the tripped guard are module state, and only the sessionStorage
 * marker survives the navigation a login restart makes.
 */
async function load() {
	vi.resetModules();
	return import("./auth.js");
}

const { bearerAuth, earlyInit } = await load();

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
		const page = await load();
		const auth = await page.bearerAuth(DEPLOYED, "/ui/");

		await auth?.onUnauthorized();

		expect(oidc.goToAuthServer).toHaveBeenCalledWith({
			redirectUrl: window.location.href,
		});
	});

	it("stops instead of looping when the fresh token is rejected too", async () => {
		vi.useFakeTimers({ now: 1_000_000, toFake: ["Date"] });
		createOidc.mockResolvedValue(loggedIn());
		const firstLoad = await load();
		await (await firstLoad.bearerAuth(DEPLOYED, "/ui/"))?.onUnauthorized();

		// Back from Keycloak: a new page load, five seconds later.
		vi.setSystemTime(1_000_000 + 5_000);
		const second = loggedIn();
		createOidc.mockResolvedValue(second);
		const secondLoad = await load();
		const auth = await secondLoad.bearerAuth(DEPLOYED, "/ui/");

		await expect(auth?.onUnauthorized()).rejects.toBeInstanceOf(
			secondLoad.LoginRejectedError,
		);
		expect(second.goToAuthServer).not.toHaveBeenCalled();
	});

	it("stays stopped for the life of the page, however long it polls", async () => {
		// The views poll every 30s and each poll reconnects. A guard that
		// expired with its window would redirect again on the next poll.
		vi.useFakeTimers({ now: 1_000_000, toFake: ["Date"] });
		createOidc.mockResolvedValue(loggedIn());
		const firstLoad = await load();
		await (await firstLoad.bearerAuth(DEPLOYED, "/ui/"))?.onUnauthorized();

		vi.setSystemTime(1_000_000 + 5_000);
		const second = loggedIn();
		createOidc.mockResolvedValue(second);
		const page = await load();
		await expect(
			(await page.bearerAuth(DEPLOYED, "/ui/"))?.onUnauthorized(),
		).rejects.toBeInstanceOf(page.LoginRejectedError);

		vi.setSystemTime(1_000_000 + 3_600_000);
		const reconnected = await page.bearerAuth(DEPLOYED, "/ui/");
		await expect(reconnected?.onUnauthorized()).rejects.toBeInstanceOf(
			page.LoginRejectedError,
		);
		expect(second.goToAuthServer).not.toHaveBeenCalled();
	});

	it("restarts once for parallel 401s, without calling it a rejection", async () => {
		const oidc = loggedIn();
		oidc.goToAuthServer.mockReturnValue(new Promise(() => {}));
		createOidc.mockResolvedValue(oidc);
		const page = await load();
		const auth = await page.bearerAuth(DEPLOYED, "/ui/");

		const outcomes: string[] = [];
		for (let i = 0; i < 3; i++) {
			auth?.onUnauthorized().then(
				() => outcomes.push("resolved"),
				() => outcomes.push("rejected"),
			);
		}
		await new Promise((resolve) => setTimeout(resolve, 0));

		expect(oidc.goToAuthServer).toHaveBeenCalledOnce();
		// All three wait on the navigation; none shows the misconfiguration message.
		expect(outcomes).toEqual([]);
	});

	it("restarts on a later page load once the loop window has passed", async () => {
		vi.useFakeTimers({ now: 1_000_000, toFake: ["Date"] });
		createOidc.mockResolvedValue(loggedIn());
		const firstLoad = await load();
		await (await firstLoad.bearerAuth(DEPLOYED, "/ui/"))?.onUnauthorized();

		// An hour later, in a new page, the session genuinely expired; that is a
		// login, not a loop.
		vi.setSystemTime(1_000_000 + 3_600_000);
		const later = loggedIn();
		createOidc.mockResolvedValue(later);
		const laterLoad = await load();
		await (await laterLoad.bearerAuth(DEPLOYED, "/ui/"))?.onUnauthorized();

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
