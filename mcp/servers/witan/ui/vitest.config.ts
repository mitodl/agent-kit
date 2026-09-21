import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config.js";

// Separate from vite.config.ts so the production build never imports vitest.
// `vite build` runs in the release workflow and the Docker node stage, and a
// config that pulled a test runner in would make both fail on any install that
// skipped devDependencies.
export default mergeConfig(
	viteConfig,
	defineConfig({
		test: {
			environment: "jsdom",
			include: ["src/**/*.test.ts"],
		},
	}),
);
