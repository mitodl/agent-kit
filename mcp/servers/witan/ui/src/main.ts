import { App } from "./app.js";
import "./style.css";

const root = document.querySelector<HTMLElement>("#app");
if (!root) {
	// index.html is ours and ships in the same bundle, so a missing mount point
	// is a build that went wrong, not a runtime condition to degrade around.
	throw new Error("#app is missing from index.html");
}

new App(root).start();
