// How the extension names `witan.server.command` when the server will not
// start. No vscode import, so the unit tests run under plain node.

/**
 * Whether `arg` holds more than one argument with an option among them, e.g.
 * `--target production` or `lsp --target production`. A path with a space
 * has no option in it, and `--target=my target` is one argument the parser
 * takes whole, so neither counts. Only `-x` / `--name` tokens are options:
 * a lone `-` in a path (`witan - Copy`) is not one.
 */
function folded(arg: unknown): boolean {
	// The setting's schema says strings, but VS Code hands back whatever the
	// settings file holds, and this runs inside the start-failure handler.
	const tokens = String(arg).trim().split(/\s+/);
	if (tokens.length < 2) {
		return false;
	}
	const options = tokens.filter((token) => /^--?[A-Za-z]/.test(token));
	if (options.length === 0) {
		return false;
	}
	const [first = ""] = tokens;
	return !(
		options.length === 1 &&
		first.startsWith("-") &&
		first.includes("=")
	);
}

/**
 * `argv` the way the setting is written, so an item holding two arguments
 * shows as one string: `["witan","lsp","--target production"]`. A plain
 * space-joined line would print `witan lsp --target production`, which looks
 * right and hides the mistake.
 */
export function describeServerCommand(argv: readonly unknown[]): string {
	const bad = argv.filter(folded);
	const hint =
		bad.length === 0
			? ""
			: ` Each item is one argument, so ${bad.map((arg) => JSON.stringify(arg)).join(", ")} ` +
				"reaches the server as a single argument: split it into one item per argument.";
	return `${JSON.stringify(argv)}.${hint}`;
}
