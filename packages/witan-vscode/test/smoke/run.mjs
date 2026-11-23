// Smoke test: the packaged .vsix in a real VS Code, against a real `witan lsp`
// on a throwaway local store.
//
// It unpacks witan.vsix (so what is tested is what ships), builds a venv of
// this repo's workspace packages from uv.lock, seeds a store and a code graph
// in a scratch HOME, and runs out/smoke/suite.cjs inside the pinned VS Code.
// Nothing reads or writes the real home: every witan path, the XDG dirs and
// HOME itself point into .vscode-test/run, and the ambient WITAN_* and
// agent-session variables are cleared first, as testsupport/hermetic.py does
// for the Python suites.
//
// Run `npm run package` first. Linux CI needs a display (xvfb-run).

import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { downloadAndUnzipVSCode, runTests } from "@vscode/test-electron";

/** The VS Code release the handoff command names are pinned against. */
const VSCODE_VERSION = "1.146.0";
const REPO = "https://github.com/test/repo";

const pkg = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const root = path.resolve(pkg, "../..");
const cache = path.join(pkg, ".vscode-test");
const work = path.join(cache, "run");
fs.rmSync(work, { recursive: true, force: true });
fs.mkdirSync(work, { recursive: true });

const realHome = os.homedir();
const home = path.join(work, "home");
const bin = path.join(work, "bin");
const repo = path.join(work, "repo");
const elsewhere = path.join(work, "elsewhere");
for (const dir of [home, bin, repo, elsewhere]) {
	fs.mkdirSync(dir, { recursive: true });
}

// ── hermetic environment ────────────────────────────────────────────────
const env = { ...process.env };
for (const name of Object.keys(env)) {
	if (
		name.startsWith("WITAN_") ||
		name.startsWith("OTEL_") ||
		[
			"CLAUDE_SESSION_ID",
			"PI_SESSION_ID",
			"CODEX_SESSION_ID",
			"CLAUDE_PROJECT_DIR",
			"CODEX_HOME",
			"CLAUDE_CONFIG_DIR",
			"SENTRY_DSN",
			"VIRTUAL_ENV",
		].includes(name)
	) {
		delete env[name];
	}
}
Object.assign(env, {
	HOME: home,
	XDG_CONFIG_HOME: path.join(home, ".config"),
	XDG_CACHE_HOME: path.join(home, ".cache"),
	XDG_DATA_HOME: path.join(home, ".local", "share"),
	XDG_STATE_HOME: path.join(home, ".local", "state"),
	WITAN_CONFIG: path.join(home, ".config", "witan", "config.toml"),
	WITAN_TOKEN_CACHE: path.join(home, ".config", "witan", "tokens.json"),
	WITAN_MERGE_WATERMARKS: path.join(
		home,
		".config",
		"witan",
		"merge-watermarks.json",
	),
	WITAN_MEMORY_URI: path.join(home, "graph.omni"),
	WITAN_CODE_DIR: path.join(home, "code"),
	WITAN_AUTHOR: "smoke@example.invalid",
	// uv's cache and Pythons are tools, not state; without these it would
	// download everything again under the scratch HOME.
	UV_CACHE_DIR: process.env.UV_CACHE_DIR ?? path.join(realHome, ".cache", "uv"),
	UV_PYTHON_INSTALL_DIR:
		process.env.UV_PYTHON_INSTALL_DIR ??
		path.join(realHome, ".local", "share", "uv", "python"),
	GIT_AUTHOR_NAME: "smoke",
	GIT_AUTHOR_EMAIL: "smoke@example.invalid",
	GIT_COMMITTER_NAME: "smoke",
	GIT_COMMITTER_EMAIL: "smoke@example.invalid",
	// Nothing from the real home: an agent CLI installed there (a real
	// `claude`) must not be what a launch finds. bin holds the fake agent and
	// a link to the omnigraph binary the store needs.
	PATH: [
		bin,
		...(process.env.PATH ?? "")
			.split(path.delimiter)
			.filter(
				(dir) => dir && !path.resolve(dir).startsWith(realHome + path.sep),
			),
	].join(path.delimiter),
});

/** `name` on the PATH this runner was started with. */
const which = (name) =>
	execFileSync("sh", ["-c", `command -v ${name}`], { encoding: "utf8" }).trim();
const uv = which("uv");
fs.symlinkSync(which("omnigraph"), path.join(bin, "omnigraph"));

const run = (file, args, options = {}) =>
	execFileSync(file, args, { env, encoding: "utf8", ...options });

// ── the server: this checkout's packages, from the lockfile ─────────────
// Its own venv (UV_PROJECT_ENVIRONMENT), so the shared workspace .venv is
// never re-synced under another package's tests.
const venv = path.join(cache, "venv");
run(
	uv,
	["sync", "--frozen", "--all-packages", "--all-extras", "--no-dev", "--quiet"],
	{
		cwd: root,
		env: { ...env, UV_PROJECT_ENVIRONMENT: venv },
		stdio: "inherit",
	},
);
const venvBin = path.join(
	venv,
	process.platform === "win32" ? "Scripts" : "bin",
);
const witan = path.join(venvBin, "witan");
const witanCode = path.join(venvBin, "witan-code");

// ── the workspace: a checkout of REPO with a little code in it ──────────
fs.mkdirSync(path.join(repo, "app"));
fs.writeFileSync(path.join(repo, "app", "__init__.py"), "");
fs.writeFileSync(
	path.join(repo, "app", "svc.py"),
	"class Service:\n    def run(self):\n        return helper()\n\n\ndef helper():\n    return 1\n",
);
fs.writeFileSync(
	path.join(repo, "app", "main.py"),
	"from app.svc import Service\n\n\ndef main():\n    return Service().run()\n",
);
run("git", ["init", "-q", "-b", "main"], { cwd: repo });
run("git", ["add", "."], { cwd: repo });
run("git", ["commit", "-q", "-m", "init"], { cwd: repo });
run("git", ["remote", "add", "origin", REPO], { cwd: repo });

// ── seed the store and the code graph ───────────────────────────────────
// `create` prints "Created task: tk-..." whatever --output-format says.
const created = (args) => {
	const out = run(witan, args, { cwd: repo });
	const slug = out.match(/\b(?:tk|wp)-[a-z0-9-]+/)?.[0];
	if (!slug) {
		throw new Error(`no slug in the output of witan ${args.join(" ")}: ${out}`);
	}
	return slug;
};
const create = (title, extra = []) =>
	created(["task", "create", "--title", title, "--repo", REPO, ...extra]);
const project = created([
	"project",
	"create",
	"--title",
	"Smoke project",
	"--repo",
	REPO,
]);
const launchTask = create("Smoke launch", ["--project", project]);
const failTask = create("Smoke failed launch");
const symbolTask = create("Smoke symbol", [
	"--symbol-refs",
	`${REPO}#app/svc.py::Service`,
]);
run(witanCode, ["index", repo], { cwd: repo, stdio: "inherit" });

// ── the fake agent: records what it was started with ────────────────────
const record = path.join(work, "agent-runs");
fs.mkdirSync(record);
const agent = path.join(bin, "fake-agent");
fs.writeFileSync(
	agent,
	`#!${process.execPath}
import * as fs from "node:fs";
import * as path from "node:path";
const picked = {};
for (const name of ${JSON.stringify([
		"CLAUDE_SESSION_ID",
		"CLAUDE_PROJECT_DIR",
		"WITAN_REPO",
		"WITAN_TARGET",
		"SMOKE_KEPT",
	])}) picked[name] = process.env[name] ?? null;
fs.writeFileSync(
	path.join(${JSON.stringify(record)}, process.pid + ".json"),
	JSON.stringify({ argv: process.argv.slice(2), cwd: process.cwd(), env: picked }),
);
`,
	{ mode: 0o755 },
);

// ── VS Code ─────────────────────────────────────────────────────────────
const userData = path.join(work, "user-data");
fs.mkdirSync(path.join(userData, "User"), { recursive: true });
fs.writeFileSync(
	path.join(userData, "User", "settings.json"),
	JSON.stringify({
		"witan.server.command": [witan, "lsp"],
		"witan.agent": "fake-agent",
		"security.workspace.trust.enabled": false,
		"telemetry.telemetryLevel": "off",
		"extensions.autoUpdate": false,
		"update.mode": "none",
	}),
);

const extension = path.join(work, "extension");
run("unzip", [
	"-q",
	path.join(pkg, "witan.vsix"),
	"-d",
	path.join(work, "vsix"),
]);
fs.renameSync(path.join(work, "vsix", "extension"), extension);

// What an editor started inside an agent session hands its terminals: the
// spec's unset_env has to take these back out, and everything else stays.
Object.assign(env, {
	CLAUDE_SESSION_ID: "outer-session",
	CLAUDE_PROJECT_DIR: elsewhere,
	SMOKE_KEPT: "kept",
});

const vscodeExecutablePath = await downloadAndUnzipVSCode({
	version: VSCODE_VERSION,
	cachePath: cache,
});
process.env = env;
await runTests({
	vscodeExecutablePath,
	extensionDevelopmentPath: extension,
	extensionTestsPath: path.join(pkg, "out", "smoke", "suite.cjs"),
	launchArgs: [
		repo,
		"--user-data-dir",
		userData,
		"--extensions-dir",
		path.join(work, "extensions"),
		"--disable-workspace-trust",
		"--skip-welcome",
		"--skip-release-notes",
	],
	extensionTestsEnv: {
		WITAN_SMOKE: JSON.stringify({
			repo,
			record,
			launchTask,
			failTask,
			symbolTask,
			project,
			symbol: `${REPO}#app/svc.py::Service`,
		}),
	},
});
