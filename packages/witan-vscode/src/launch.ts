// The launch contract as this client runs it (ADR 0012, "3. The launch
// contract"). No vscode import, so the unit tests run under plain node.

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
 * The `env` for `vscode.window.createTerminal`: every `unset_env` name as
 * `null`, which removes it from the environment the terminal would otherwise
 * get, then `env` over that. Never paired with `strictEnv`, so everything else
 * the person's terminals get is kept.
 */
export function terminalEnv(spec: LaunchSpec): Record<string, string | null> {
	const env: Record<string, string | null> = {};
	for (const name of spec.unset_env) {
		env[name] = null;
	}
	return { ...env, ...spec.env };
}

/** `base` as the launched process will see it: `unset_env` removed, `env` set. */
export function effectiveEnv(
	base: Readonly<Record<string, string | undefined>>,
	spec: LaunchSpec,
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
 * A command with a directory in it is taken as a path. Checked before the
 * terminal opens, because a terminal whose executable is missing shows an
 * error the extension cannot see, and the claim would sit held for the lease.
 */
export function findExecutable(
	command: string,
	env: Readonly<Record<string, string>>,
	platform: NodeJS.Platform,
	isExecutable: (file: string) => boolean,
): string | undefined {
	const lib = platform === "win32" ? path.win32 : path.posix;
	const extensions =
		platform === "win32"
			? ["", ...(pathValue(env, "PATHEXT") ?? ".COM;.EXE;.BAT;.CMD").split(";")]
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
