// Bundles the extension, and the smoke suite VS Code loads to test it, into
// single CommonJS files. The extension host requires `main`, and the bundle
// carries vscode-languageclient, so the .vsix ships no node_modules.
import * as esbuild from "esbuild";

const common = {
	bundle: true,
	platform: "node",
	format: "cjs",
	target: "node20",
	external: ["vscode"],
	sourcemap: true,
	logLevel: "info",
};

await esbuild.build({
	...common,
	entryPoints: ["src/extension.ts"],
	outfile: "dist/extension.cjs",
	minify: true,
});
await esbuild.build({
	...common,
	entryPoints: ["src/launcher.ts"],
	outfile: "dist/launcher.cjs",
	minify: true,
	sourcemap: false,
});
await esbuild.build({
	...common,
	entryPoints: ["test/smoke/suite.ts"],
	outfile: "out/smoke/suite.cjs",
});
