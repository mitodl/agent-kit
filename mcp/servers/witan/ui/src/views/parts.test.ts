import { html, render } from "lit-html";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { copyButton } from "./parts.js";

let root: HTMLElement;

beforeEach(() => {
	root = document.createElement("div");
	document.body.replaceChildren(root);
});

afterEach(() => {
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

function button(): HTMLButtonElement {
	const found = root.querySelector<HTMLButtonElement>("button.copy");
	if (!found) {
		throw new Error("no copy button rendered");
	}
	return found;
}

describe("copyButton", () => {
	it("copies the slug, not the label", async () => {
		const writeText = vi.fn(async () => {});
		vi.stubGlobal("navigator", { clipboard: { writeText } });
		render(copyButton("tk-some-task-abc123", "the task"), root);

		button().click();

		expect(writeText).toHaveBeenCalledWith("tk-some-task-abc123");
		await vi.waitFor(() => expect(button().dataset.copied).toBe("yes"));
		expect(button().getAttribute("aria-label")).toBe("Copy the task");
	});

	it("does not follow a link it sits inside", () => {
		vi.stubGlobal("navigator", { clipboard: { writeText: async () => {} } });
		render(html`<a href="#/elsewhere">${copyButton("wp-x")}</a>`, root);
		const event = new MouseEvent("click", { bubbles: true, cancelable: true });

		button().dispatchEvent(event);

		expect(event.defaultPrevented).toBe(true);
	});

	it("falls back to the copy command where the Clipboard API is missing", async () => {
		// jsdom has no execCommand, so define one to stand in for the browser's.
		const execCommand = vi.fn(() => true);
		Object.defineProperty(document, "execCommand", {
			value: execCommand,
			configurable: true,
		});
		vi.stubGlobal("navigator", {});
		render(copyButton("wp-x"), root);

		button().click();

		await vi.waitFor(() => expect(button().dataset.copied).toBe("yes"));
		expect(execCommand).toHaveBeenCalledWith("copy");
		expect(document.querySelector("textarea")).toBeNull();
	});

	it("falls back when the Clipboard API refuses, as an iframe without clipboard-write does", async () => {
		// A permissions denial is a rejected promise, not a missing API.
		const execCommand = vi.fn(() => true);
		Object.defineProperty(document, "execCommand", {
			value: execCommand,
			configurable: true,
		});
		vi.stubGlobal("navigator", {
			clipboard: {
				writeText: async () => {
					throw new DOMException("denied", "NotAllowedError");
				},
			},
		});
		render(copyButton("wp-x"), root);

		button().click();

		await vi.waitFor(() => expect(button().dataset.copied).toBe("yes"));
		expect(execCommand).toHaveBeenCalledWith("copy");
	});

	it("announces a failed copy rather than only colouring it", async () => {
		Object.defineProperty(document, "execCommand", {
			value: () => false,
			configurable: true,
		});
		vi.stubGlobal("navigator", {});
		render(copyButton("wp-x"), root);

		button().click();

		await vi.waitFor(() => expect(button().dataset.copied).toBe("no"));
		expect(root.querySelector('[role="status"]')?.textContent).toBe(
			"Copy failed",
		);
	});

	it("clears the copied mark after a moment", async () => {
		vi.useFakeTimers();
		vi.stubGlobal("navigator", { clipboard: { writeText: async () => {} } });
		render(copyButton("wp-x"), root);

		button().click();
		await vi.waitFor(() => expect(button().dataset.copied).toBe("yes"));
		expect(root.querySelector('[role="status"]')?.textContent).toBe("Copied");
		vi.advanceTimersByTime(1500);

		expect(button().dataset.copied).toBeUndefined();
		expect(root.querySelector('[role="status"]')?.textContent).toBe("");
	});
});
