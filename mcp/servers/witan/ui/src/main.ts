import { render } from "lit-html";
import { placeholder, shell, viewFromHash } from "./shell.js";
import "./style.css";

const root = document.querySelector<HTMLElement>("#app");
if (!root) {
	// index.html is ours and ships in the same bundle, so a missing mount point
	// is a build that went wrong, not a runtime condition to degrade around.
	throw new Error("#app is missing from index.html");
}

function draw(): void {
	// `root` is narrowed above; the closure needs the non-null form.
	render(
		shell(viewFromHash(window.location.hash), placeholder()),
		root as HTMLElement,
	);
}

window.addEventListener("hashchange", draw);
draw();
