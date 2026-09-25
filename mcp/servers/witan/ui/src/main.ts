import { App } from "./app.js";
import { earlyInit } from "./auth.js";
import { mountBase } from "./mcp.js";
// The page's typefaces, self-hosted because the CSP is `default-src 'self'`.
// Imported here rather than from style.css so the MCP Apps widgets, which
// share the stylesheet but are built single-file, do not inline ~130 KB of
// base64 fonts a host's CSP may refuse anyway. They fall back to the system
// stack named after Alegreya in `--font-*`.
import "@fontsource/alegreya-sans/latin-400.css";
import "@fontsource/alegreya-sans/latin-400-italic.css";
import "@fontsource/alegreya-sans/latin-500.css";
import "@fontsource/alegreya-sans/latin-700.css";
import "@fontsource/alegreya/latin-500.css";
import "@fontsource/alegreya/latin-700.css";
import "./style.css";

// Before anything else touches the URL: a login redirect lands here with the
// authorization code in the fragment, where the router would read it as a
// route.
const shouldStart = earlyInit(mountBase(document.baseURI));

const root = document.querySelector<HTMLElement>("#app");
if (!root) {
	// index.html is ours and ships in the same bundle, so a missing mount point
	// is a build that went wrong, not a runtime condition to degrade around.
	throw new Error("#app is missing from index.html");
}

if (shouldStart) {
	new App(root).start();
}
