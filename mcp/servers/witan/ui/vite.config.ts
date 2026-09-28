import { defineConfig, type Plugin } from "vite";

// oidc-spa declares `"sideEffects": false` for the whole package, but its
// vendored webcrypto-liner shim exists only to polyfill `crypto.subtle` when
// the page is not a secure context. Rolldown (vite 8) honors the declaration
// on a dynamic import and emits the shim's chunk empty, so login would fail
// over plain http with nothing in the build output to say why.
const keepWebcryptoLinerShim: Plugin = {
	name: "keep-webcrypto-liner-shim",
	transform(code, id) {
		if (id.includes("/oidc-spa/") && id.endsWith("/webcrypto-liner-shim.mjs")) {
			return { code, moduleSideEffects: true };
		}
		return null;
	},
};

export default defineConfig({
	plugins: [keepWebcryptoLinerShim],
	// The bundle is mounted under /ui/ on the witan process (spec §5.2), so
	// asset URLs have to be written relative to that prefix. Vite's default "/"
	// would emit /assets/… and 404 against the MCP endpoint's origin.
	base: "/ui/",
	build: {
		// Inside the Python package, not beside it: `packages = ["witan"]` is what
		// puts files in the wheel, and hatch's `artifacts` setting (pyproject.toml)
		// is what stops .gitignore from excluding a build output. Anywhere else and
		// the bundle would have to be force-included by hand.
		outDir: "../witan/ui_dist",
		emptyOutDir: true,
	},
});
