import assert from "node:assert/strict";
import { test } from "node:test";
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
	terminalEnv,
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

test("the terminal env removes every unset_env name and sets env over it", () => {
	assert.deepEqual(
		terminalEnv(
			spec({ unset_env: ["CLAUDE_SESSION_ID", "WITAN_TARGET"], env: {} }),
		),
		{ CLAUDE_SESSION_ID: null, WITAN_TARGET: null },
	);
	// A name in both: env wins, which is how the spec pins a target.
	assert.deepEqual(
		terminalEnv(
			spec({ unset_env: ["WITAN_TARGET"], env: { WITAN_TARGET: "qa" } }),
		),
		{ WITAN_TARGET: "qa" },
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
	);
	assert.deepEqual(env, {
		PATH: "/usr/bin",
		HOME: "/home/me",
		WITAN_TARGET: "prod",
	});
});

test("the agent is looked up on the launched env's PATH", () => {
	const present = new Set(["/opt/agents/claude", "/usr/bin/git"]);
	const exists = (file: string) => present.has(file);
	const env = { PATH: "/usr/bin:/opt/agents" };
	assert.equal(
		findExecutable("claude", env, "linux", exists),
		"/opt/agents/claude",
	);
	assert.equal(findExecutable("codex", env, "linux", exists), undefined);
	assert.equal(
		findExecutable("/opt/agents/claude", {}, "linux", exists),
		"/opt/agents/claude",
	);
	assert.equal(findExecutable("claude", {}, "linux", exists), undefined);
});

test("on Windows the lookup honours PATHEXT and any spelling of PATH", () => {
	const present = new Set(["C:\\tools\\claude.CMD"]);
	const exists = (file: string) => present.has(file);
	assert.equal(
		findExecutable(
			"claude",
			{ Path: "C:\\Windows;C:\\tools", PATHEXT: ".EXE;.CMD" },
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
