import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import {
	availableHandoffs,
	CLAUDE_HANDOFF_COMMAND,
	COPILOT_HANDOFF_COMMAND,
	handoffArguments,
} from "../../src/handoff.ts";
import {
	checkVersion,
	effectiveEnv,
	findExecutable,
	type LaunchSpec,
	UnsupportedSpecVersion,
} from "../../src/launch.ts";

function spec(overrides: Partial<LaunchSpec> = {}): LaunchSpec {
	return {
		version: 1,
		kind: "task",
		slug: "tk-a",
		launchable: true,
		argv: ["claude", "--model", "opus", "do the thing"],
		cwd: "/home/me/repo",
		env: { WITAN_TARGET: "prod" },
		unset_env: [
			"CLAUDE_SESSION_ID",
			"PI_SESSION_ID",
			"CODEX_SESSION_ID",
			"CLAUDE_PROJECT_DIR",
			"WITAN_REPO",
		],
		handoff: false,
		prompt: "do the thing",
		agent: "claude",
		model: "opus",
		claim: { claimed: true, claimed_at: "2026-11-17T16:20:04.607+00:00" },
		release: {
			slug: "tk-a",
			claimed_at: "2026-11-17T16:20:04.607+00:00",
			uri: "file:///home/me/repo",
		},
		...overrides,
	};
}

test("a spec of an unknown version is refused", () => {
	checkVersion({ version: 1 });
	assert.throws(() => checkVersion({ version: 2 }), UnsupportedSpecVersion);
	assert.throws(() => checkVersion({ version: "1" }), UnsupportedSpecVersion);
	assert.throws(
		() => checkVersion({ version: undefined }),
		/version undefined/,
	);
});

test("the effective env keeps everything the terminal would get otherwise", () => {
	const env = effectiveEnv(
		{
			PATH: "/usr/bin",
			HOME: "/home/me",
			CLAUDE_SESSION_ID: "outer",
			CLAUDE_PROJECT_DIR: "/elsewhere",
			WITAN_TARGET: "personal",
			UNSET: undefined,
		},
		spec(),
		"linux",
	);
	assert.deepEqual(env, {
		PATH: "/usr/bin",
		HOME: "/home/me",
		WITAN_TARGET: "prod",
	});
});

test("on POSIX env names are case-sensitive", () => {
	const env = effectiveEnv(
		{ Witan_Target: "kept", Claude_Session_Id: "kept" },
		spec(),
		"linux",
	);
	assert.deepEqual(env, {
		Witan_Target: "kept",
		Claude_Session_Id: "kept",
		WITAN_TARGET: "prod",
	});
});

test("on Windows env names match in any case, for removal and overlay", () => {
	const env = effectiveEnv(
		{
			Path: "C:\\Windows",
			Witan_Target: "personal",
			Claude_Session_Id: "outer",
		},
		spec(),
		"win32",
	);
	assert.deepEqual(env, { Path: "C:\\Windows", WITAN_TARGET: "prod" });
});

test("the agent is looked up on the launched env's PATH", () => {
	const present = new Set(["/opt/agents/claude", "/usr/bin/git"]);
	const exists = (file: string) => present.has(file);
	const env = { PATH: "/usr/bin:/opt/agents" };
	const cwd = "/home/me/repo";
	assert.equal(
		findExecutable("claude", env, cwd, "linux", exists),
		"/opt/agents/claude",
	);
	assert.equal(findExecutable("codex", env, cwd, "linux", exists), undefined);
	assert.equal(
		findExecutable("/opt/agents/claude", {}, cwd, "linux", exists),
		"/opt/agents/claude",
	);
	assert.equal(findExecutable("claude", {}, cwd, "linux", exists), undefined);
});

test("relative commands and PATH entries resolve against the spawn cwd", () => {
	const present = new Set([
		"/home/me/repo/node_modules/.bin/claude",
		"/home/me/repo/bin/pi",
	]);
	const exists = (file: string) => present.has(file);
	const env = { PATH: "node_modules/.bin:/usr/bin" };
	assert.equal(
		findExecutable("claude", env, "/home/me/repo", "linux", exists),
		"/home/me/repo/node_modules/.bin/claude",
	);
	assert.equal(
		findExecutable("./bin/pi", {}, "/home/me/repo", "linux", exists),
		"/home/me/repo/bin/pi",
	);
	// The same names from another folder are other files, or none.
	assert.equal(
		findExecutable("claude", env, "/home/me/other", "linux", exists),
		undefined,
	);
});

test("on Windows the lookup honours PATHEXT and any spelling of PATH", () => {
	const present = new Set(["C:\\tools\\claude", "C:\\tools\\claude.CMD"]);
	const exists = (file: string) => present.has(file);
	assert.equal(
		findExecutable(
			"claude",
			{ Path: "C:\\Windows;C:\\tools", PATHEXT: ".EXE;.CMD" },
			"C:\\repo",
			"win32",
			exists,
		),
		"C:\\tools\\claude.CMD",
	);
});

test("handoff targets follow the registered commands", () => {
	assert.deepEqual(availableHandoffs([]), { copilot: false, claude: false });
	assert.deepEqual(availableHandoffs([COPILOT_HANDOFF_COMMAND, "other"]), {
		copilot: true,
		claude: false,
	});
	assert.deepEqual(availableHandoffs([CLAUDE_HANDOFF_COMMAND]), {
		copilot: false,
		claude: true,
	});
});

test("each handoff command gets the prompt the way it takes it", () => {
	assert.deepEqual(handoffArguments("copilot", "p"), [{ prompt: "p" }]);
	assert.deepEqual(handoffArguments("claude", "p"), [undefined, "p"]);
});

const LAUNCHER = fileURLToPath(
	new URL("../../src/launcher.ts", import.meta.url),
);

/** Run the launcher on a launch file, with `bin` as the whole PATH. */
function runLauncher(argv: string[]) {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "witan-launcher-"));
	const bin = path.join(dir, "bin");
	fs.mkdirSync(bin);
	const record = path.join(dir, "record.json");
	fs.writeFileSync(
		path.join(bin, "fake-agent"),
		`#!${process.execPath}\n` +
			"require('node:fs').writeFileSync(process.env.RECORD, JSON.stringify(" +
			"{ argv: process.argv.slice(2), cwd: process.cwd(), env: process.env }));\n",
		{ mode: 0o755 },
	);
	// No package.json above the agent says "module", so require() works.
	fs.writeFileSync(path.join(bin, "package.json"), '{"type": "commonjs"}');
	const file = path.join(dir, "launch.json");
	const status = path.join(dir, "status.json");
	fs.writeFileSync(
		file,
		JSON.stringify({
			argv,
			cwd: dir,
			env: { WITAN_TARGET: "prod" },
			unset_env: ["CLAUDE_SESSION_ID"],
			status,
		}),
	);
	const result = spawnSync(process.execPath, [LAUNCHER, file], {
		env: {
			PATH: bin,
			RECORD: record,
			CLAUDE_SESSION_ID: "outer",
			ELECTRON_RUN_AS_NODE: "1",
			KEPT: "kept",
		},
		encoding: "utf8",
	});
	const read = (f: string) =>
		fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, "utf8")) : undefined;
	const out = {
		code: result.status,
		stderr: result.stderr,
		launchFileLeft: fs.existsSync(file),
		status: read(status),
		agent: read(record),
	};
	fs.rmSync(dir, { recursive: true, force: true });
	return out;
}

test("the launcher passes the prompt through untouched and applies the spec's env", {
	skip: process.platform === "win32",
}, () => {
	// biome-ignore lint/suspicious/noTemplateCurlyInString: the literal is the point.
	const prompt = "Fix ${env:KEPT} in ${workspaceFolder}\nand `$(rm -rf /)`";
	const run = runLauncher(["fake-agent", "--model", "opus", prompt]);
	assert.equal(run.code, 0, run.stderr);
	assert.equal(run.launchFileLeft, false);
	assert.equal(typeof run.status.started, "number");
	assert.deepEqual(run.agent.argv, ["--model", "opus", prompt]);
	assert.equal(run.agent.env.WITAN_TARGET, "prod");
	assert.equal(run.agent.env.KEPT, "kept");
	assert.equal(run.agent.env.CLAUDE_SESSION_ID, undefined);
	assert.equal(run.agent.env.ELECTRON_RUN_AS_NODE, undefined);
});

test("the launcher reports an agent that is not on PATH", () => {
	const run = runLauncher(["no-such-agent", "prompt"]);
	assert.equal(run.code, 127);
	assert.match(run.status.error, /no-such-agent is not on the PATH/);
	assert.equal(run.agent, undefined);
});
