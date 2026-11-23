// The launch contract as this client runs it (ADR 0012, "3. The launch
// contract"). No vscode import, so the unit tests run under plain node.

import * as fs from "node:fs";
import * as path from "node:path";

/** The launch-spec versions this client knows how to run. */
export const SUPPORTED_SPEC_VERSIONS: readonly number[] = [1];

export type LaunchKind = "task" | "project";

/** What `witan.releaseTask` takes, passed back exactly as the spec gave it. */
export interface ReleaseArgs {
	slug: string;
	claimed_at: string;
	uri: string;
}

export interface LaunchSpec {
	version: number;
	kind: LaunchKind;
	slug: string;
	launchable: true;
	argv: string[];
	cwd: string;
	env: Record<string, string>;
	unset_env: string[];
	handoff: boolean;
	prompt: string;
	agent: string;
	model: string | null;
	claim: Record<string, unknown> | null;
	release?: ReleaseArgs;
}

export interface LaunchRefusal {
	version: number;
	kind: LaunchKind;
	slug: string;
	launchable: false;
	reason: string;
	message: string;
	claim: Record<string, unknown> | null;
}

export type LaunchResult = LaunchSpec | LaunchRefusal;

export class UnsupportedSpecVersion extends Error {}

/**
 * Refuse a spec this client does not understand rather than guess at it. A
 * version bump means a field was removed or changed meaning.
 */
export function checkVersion(result: { version: unknown }): void {
	if (
		typeof result.version !== "number" ||
		!SUPPORTED_SPEC_VERSIONS.includes(result.version)
	) {
		throw new UnsupportedSpecVersion(
			`witan returned a launch spec of version ${String(result.version)}, and this ` +
				`extension runs version ${SUPPORTED_SPEC_VERSIONS.join(", ")}. Update the witan extension.`,
		);
	}
}

/**
 * What the extension hands the launcher (src/launcher.ts) in a file: the spec's
 * fields that would otherwise be terminal arguments, and where to report back.
 */
export interface LaunchFile {
	argv: string[];
	cwd: string;
	env: Record<string, string>;
	unset_env: string[];
	/** Where the launcher writes a {@link LaunchStatus}. */
	status: string;
}

/** The launcher's report: the agent started, or why it did not. */
export type LaunchStatus = { started: number } | { error: string };

/**
 * `base` (the environment the terminal gave the launcher) as the agent gets
 * it: `unset_env` removed, then `env` set over it. Everything else the
 * person's terminals get is kept, which is what a strict env would lose.
 */
export function effectiveEnv(
	base: Readonly<Record<string, string | undefined>>,
	spec: Pick<LaunchSpec, "env" | "unset_env">,
): Record<string, string> {
	const unset = new Set(spec.unset_env);
	const env: Record<string, string> = {};
	for (const [name, value] of Object.entries(base)) {
		if (value !== undefined && !unset.has(name)) {
			env[name] = value;
		}
	}
	return { ...env, ...spec.env };
}

/**
 * Where `command` resolves on `env`'s PATH, or `undefined` when it does not.
 * A command with a directory in it is taken as a path.
 */
export function findExecutable(
	command: string,
	env: Readonly<Record<string, string>>,
	platform: NodeJS.Platform,
	isExecutable: (file: string) => boolean,
): string | undefined {
	const lib = platform === "win32" ? path.win32 : path.posix;
	// On Windows a bare name runs only with a PATHEXT extension: npm puts an
	// extensionless sh script for Git Bash next to every `.cmd` shim.
	const extensions =
		platform === "win32" && lib.extname(command) === ""
			? (pathValue(env, "PATHEXT") ?? ".COM;.EXE;.BAT;.CMD").split(";")
			: [""];
	const candidates =
		command.includes(lib.sep) || command.includes("/")
			? [command]
			: (pathValue(env, "PATH") ?? "")
					.split(lib.delimiter)
					.filter(Boolean)
					.map((dir) => lib.join(dir, command));
	for (const candidate of candidates) {
		for (const ext of extensions) {
			if (isExecutable(candidate + ext)) {
				return candidate + ext;
			}
		}
	}
	return undefined;
}

/** Windows spells PATH in any case. */
function pathValue(
	env: Readonly<Record<string, string>>,
	name: string,
): string | undefined {
	const key = Object.keys(env).find((k) => k.toUpperCase() === name);
	return key === undefined ? undefined : env[key];
}

export function isExecutable(file: string): boolean {
	try {
		fs.accessSync(file, fs.constants.X_OK);
		return fs.statSync(file).isFile();
	} catch {
		return false;
	}
}
