import { describe, expect, it, vi } from "vitest";
import { isStale, LiveRead, type Snapshot } from "./live.js";

/** A read whose resolution the test controls, one call at a time. */
function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (cause: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

function collect<T>() {
	const seen: Snapshot<T>[] = [];
	return { seen, notify: (snapshot: Snapshot<T>) => seen.push(snapshot) };
}

describe("LiveRead", () => {
	it("reads once on start and reports the result", async () => {
		const { seen, notify } = collect<string>();
		const live = new LiveRead(() => Promise.resolve("ok"), notify);

		live.start();
		await vi.waitFor(() => expect(live.snapshot.data).toBe("ok"));
		live.stop();

		expect(live.snapshot.loading).toBe(false);
		expect(live.snapshot.loadedAt).not.toBeNull();
		// The in-flight state is reported too, so a view can say it is reading.
		expect(seen.some((snapshot) => snapshot.loading)).toBe(true);
	});

	it("keeps the last good data when a later read fails", async () => {
		let attempt = 0;
		const live = new LiveRead(
			() => {
				attempt += 1;
				return attempt === 1
					? Promise.resolve("first")
					: Promise.reject(new Error("server went away"));
			},
			() => {},
		);

		live.start();
		await vi.waitFor(() => expect(live.snapshot.data).toBe("first"));
		live.refresh();
		await vi.waitFor(() => expect(live.snapshot.error).not.toBeNull());
		live.stop();

		// The whole point of the module: a failed refresh marks the data stale,
		// it does not blank it.
		expect(live.snapshot.data).toBe("first");
		expect(isStale(live.snapshot)).toBe(true);
	});

	it("clears the error on the next success", async () => {
		let attempt = 0;
		const live = new LiveRead(
			() => {
				attempt += 1;
				return attempt === 1
					? Promise.reject(new Error("transient"))
					: Promise.resolve("recovered");
			},
			() => {},
		);

		live.start();
		await vi.waitFor(() => expect(live.snapshot.error).not.toBeNull());
		live.refresh();
		await vi.waitFor(() => expect(live.snapshot.data).toBe("recovered"));
		live.stop();

		expect(live.snapshot.error).toBeNull();
		expect(isStale(live.snapshot)).toBe(false);
	});

	it("drops a slow read that a newer one overtook", async () => {
		const slow = deferred<string>();
		const fast = deferred<string>();
		const reads = [slow, fast];
		let index = 0;
		const live = new LiveRead(
			() => {
				const next = reads[index++];
				if (!next) {
					throw new Error("read called more than the test set up");
				}
				return next.promise;
			},
			() => {},
		);

		live.start();
		live.refresh();

		fast.resolve("second");
		await vi.waitFor(() => expect(live.snapshot.data).toBe("second"));
		// The first read lands last. Without the generation check it would
		// overwrite the newer result with an older one.
		slow.resolve("first");
		await Promise.resolve();
		live.stop();

		expect(live.snapshot.data).toBe("second");
	});

	it("ignores a read that resolves after stop", async () => {
		const pending = deferred<string>();
		const { seen, notify } = collect<string>();
		const live = new LiveRead(() => pending.promise, notify);

		live.start();
		live.stop();
		pending.resolve("late");
		await Promise.resolve();

		expect(live.snapshot.data).toBeNull();
		expect(seen.every((snapshot) => snapshot.data === null)).toBe(true);
	});

	it("blanks the snapshot on retarget", async () => {
		const live = new LiveRead(
			() => Promise.resolve("a"),
			() => {},
		);
		live.start();
		await vi.waitFor(() => expect(live.snapshot.data).toBe("a"));

		const second = deferred<string>();
		live.retarget(() => second.promise);
		// Retargeting means the filters moved, so the retained data now answers a
		// question nobody asked. That is the one case where blanking is right.
		expect(live.snapshot.data).toBeNull();
		expect(live.snapshot.loading).toBe(true);

		second.resolve("b");
		await vi.waitFor(() => expect(live.snapshot.data).toBe("b"));
		live.stop();
	});

	it("re-reads when the window regains focus", async () => {
		const read = vi.fn(() => Promise.resolve("ok"));
		const live = new LiveRead(read, () => {});

		live.start();
		await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(1));
		window.dispatchEvent(new Event("focus"));
		await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(2));
		live.stop();

		// And the listener is detached: a stopped read must not keep polling.
		window.dispatchEvent(new Event("focus"));
		expect(read).toHaveBeenCalledTimes(2);
	});

	it("polls on the interval", async () => {
		vi.useFakeTimers();
		try {
			const read = vi.fn(() => Promise.resolve("ok"));
			const live = new LiveRead(read, () => {}, { intervalMs: 1000 });

			live.start();
			expect(read).toHaveBeenCalledTimes(1);
			vi.advanceTimersByTime(3000);
			expect(read).toHaveBeenCalledTimes(4);
			live.stop();
			vi.advanceTimersByTime(3000);
			expect(read).toHaveBeenCalledTimes(4);
		} finally {
			vi.useRealTimers();
		}
	});

	it("runs no interval when one is disabled", async () => {
		vi.useFakeTimers();
		try {
			const read = vi.fn(() => Promise.resolve("ok"));
			// Spec §6.6: the retrospective timeline re-reads on focus and on a
			// manual refresh, never on the interval.
			const live = new LiveRead(read, () => {}, { intervalMs: 0 });

			live.start();
			vi.advanceTimersByTime(600_000);
			live.stop();

			expect(read).toHaveBeenCalledTimes(1);
		} finally {
			vi.useRealTimers();
		}
	});
});
