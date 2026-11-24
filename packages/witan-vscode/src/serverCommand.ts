// How the extension names `witan.server.command` when the server will not
// start. No vscode import, so the unit tests run under plain node.

/**
 * `argv` the way the setting is written, so an item holding two arguments
 * shows as one string: `["witan","lsp","--target production"]`. A plain
 * space-joined line would print `witan lsp --target production`, which looks
 * right and hides the mistake.
 */
export function describeServerCommand(argv: readonly string[]): string {
	const folded = argv.filter((arg) => /^-\S*\s/.test(arg));
	const hint =
		folded.length === 0
			? ""
			: ` Each item is one argument, so ${folded.map((arg) => JSON.stringify(arg)).join(", ")} ` +
				"reaches the server as a single argument: put the option and its value in separate items.";
	return `${JSON.stringify(argv)}.${hint}`;
}
