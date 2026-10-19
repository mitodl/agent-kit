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

	it("gives focus back to the button after the fallback copy", async () => {
		// Chromium moves focus to the textarea when it is selected; jsdom does
		// not, so the stand-in for the browser's copy does it instead.
		Object.defineProperty(document, "execCommand", {
			value: () => {
				document.querySelector("textarea")?.focus();
				return true;
			},
			configurable: true,
		});
		vi.stubGlobal("navigator", {});
		render(copyButton("wp-x"), root);
		button().focus();

		button().click();

		await vi.waitFor(() => expect(button().dataset.copied).toBe("yes"));
		expect(document.activeElement).toBe(button());
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

	it("keeps a second copy's result for the full moment", async () => {
		vi.useFakeTimers();
		Object.defineProperty(document, "execCommand", {
			value: () => false,
			configurable: true,
		});
		const writeText = vi
			.fn<() => Promise<void>>()
			.mockResolvedValueOnce()
			.mockRejectedValueOnce(new DOMException("denied", "NotAllowedError"));
		vi.stubGlobal("navigator", { clipboard: { writeText } });
		render(copyButton("wp-x"), root);

		button().click();
		await vi.waitFor(() => expect(button().dataset.copied).toBe("yes"));
		vi.advanceTimersByTime(1000);
		button().click();
		await vi.waitFor(() => expect(button().dataset.copied).toBe("no"));
		// The first copy's reset would land here, 500ms into the second result.
		vi.advanceTimersByTime(600);

		expect(button().dataset.copied).toBe("no");
		expect(root.querySelector('[role="status"]')?.textContent).toBe(
			"Copy failed",
		);
		vi.advanceTimersByTime(900);
		expect(button().dataset.copied).toBeUndefined();
	});

	it("drops the copied mark when a re-render puts another slug in its place", async () => {
		// An unkeyed list re-rendered after a reorder reuses the row's DOM, so
		// the button that just copied one slug now sits beside another.
		vi.stubGlobal("navigator", { clipboard: { writeText: async () => {} } });
		render(copyButton("tk-first"), root);
		button().click();
		await vi.waitFor(() => expect(button().dataset.copied).toBe("yes"));

		render(copyButton("tk-second"), root);

		expect(button().dataset.copied).toBeUndefined();
		expect(root.querySelector('[role="status"]')?.textContent).toBe("");
		expect(button().getAttribute("aria-label")).toBe("Copy tk-second");
	});

	it("keeps the copied mark across a re-render of the same slug", async () => {
		vi.stubGlobal("navigator", { clipboard: { writeText: async () => {} } });
		render(copyButton("tk-first"), root);
		button().click();
		await vi.waitFor(() => expect(button().dataset.copied).toBe("yes"));

		render(copyButton("tk-first"), root);

		expect(button().dataset.copied).toBe("yes");
	});
});
