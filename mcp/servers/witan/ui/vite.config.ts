import { defineConfig } from "vite";

export default defineConfig({
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
