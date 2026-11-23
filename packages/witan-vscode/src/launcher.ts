// The process of a terminal the extension opens for a launch: it starts the
// agent from a launch file and stays its parent until it exits.
//
// The agent's argv, cwd and env travel in the file rather than as the
// terminal's own args, env and cwd, because VS Code resolves `${...}`
// variables in those. The prompt carries teammate-written text, so a task
// titled `${env:GITHUB_TOKEN}` would reach the agent with the token in it.
// Looking the agent up here also uses the PATH this terminal really has,
// after VS Code applied the person's terminal settings.

import { spawn } from "node:child_process";
import * as fs from "node:fs";
import {
	effectiveEnv,
	findExecutable,
	isExecutable,
	type LaunchFile,
	type LaunchStatus,
} from "./launch.ts";

/** The shell's "command not found", so a person reading the exit code knows. */
const NOT_STARTED_EXIT = 127;

const file = process.argv[2] ?? "";
const launch = JSON.parse(fs.readFileSync(file, "utf8")) as LaunchFile;
fs.rmSync(file);

function report(status: LaunchStatus): void {
	// Renamed into place, so the extension never reads half a file.
	fs.writeFileSync(`${launch.status}.tmp`, JSON.stringify(status));
	fs.renameSync(`${launch.status}.tmp`, launch.status);
}

function fail(message: string): never {
	report({ error: message });
	process.stderr.write(`witan: ${message}\n`);
	process.exit(NOT_STARTED_EXIT);
}

const base = { ...process.env };
// Only this launcher runs as node; the agent is a normal process.
delete base.ELECTRON_RUN_AS_NODE;
const env = effectiveEnv(base, launch);
const [program = "", ...args] = launch.argv;
const executable = findExecutable(program, env, process.platform, isExecutable);
if (executable === undefined) {
	fail(
		`${program} is not on the PATH a terminal gets. Install it, or set witan.agent.`,
	);
}
if (process.platform === "win32" && /\.(cmd|bat)$/i.test(executable)) {
	// Node runs a batch file only through cmd.exe, which would reparse the
	// prompt's quotes, newlines, `&` and `|`.
	fail(
		`${executable} is a batch-file shim, which cannot be given the prompt intact. Point witan.agent at the agent's executable.`,
	);
}

const child = spawn(executable, args, {
	cwd: launch.cwd,
	env,
	stdio: "inherit",
});
child.on("spawn", () => report({ started: child.pid ?? 0 }));
child.on("error", (err) => fail(err.message));
// Ctrl-C reaches the agent from the terminal itself; it decides what it means.
process.on("SIGINT", () => undefined);
for (const signal of ["SIGTERM", "SIGHUP"] as const) {
	process.on(signal, () => child.kill(signal));
}
child.on("exit", (code, signal) => {
	if (signal) {
		process.removeAllListeners(signal);
		process.kill(process.pid, signal);
		return;
	}
	process.exit(code ?? 1);
});
