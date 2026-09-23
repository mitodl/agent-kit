import { App } from "./app.js";
import { earlyInit } from "./auth.js";
import { mountBase } from "./mcp.js";
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
