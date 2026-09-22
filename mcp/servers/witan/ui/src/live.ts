/**
 * Polling with last-good retention, for the live views.
 *
 * Witan has no change feed, and the stateless protocol era (ADR 0009) has no
 * server→client channel, so spec §6.1 settles for polling: every live view
 * re-reads on an interval and on window focus, shows the read time, and on a
 * failed read keeps the last good data on screen marked stale rather than
 * blanking it.
 *
 * That last clause is the whole reason this module exists rather than each
 * view owning a `setInterval`. A board that empties itself because one poll
 * hit a restarting server reads as "nothing is ready", which is a lie a person
 * acts on.
 */

/** What a view renders from: the last good data, plus how it is doing. */
export interface Snapshot<T> {
	/** The most recent SUCCESSFUL result. Retained across failures. */
	data: T | null;
	/** The most recent failure, cleared by the next success. */
	error: Error | null;
	/** When `data` was read, as epoch ms. `null` before the first success. */
	loadedAt: number | null;
	/** A read is in flight. True during the first one, so views can say so. */
	loading: boolean;
}

/**
 * Whether a read has come back at all.
 *
 * ★ `data !== null` IS NOT THIS QUESTION, and getting the two confused is a
 * real bug rather than a style point: `task_get` of a missing slug returns
 * `null` as its VALUE, so a panel that tests `data` shows "Reading…" forever
 * for every stale link. `loadedAt` is set only by a success, so it separates
 * "nothing yet" from "the graph says no".
 */
export function hasResult<T>(snapshot: Snapshot<T>): boolean {
	return snapshot.loadedAt !== null;
}

/**
 * Data on screen that a later read failed to refresh.
 *
 * Not a stored field: it is exactly "we have something to show and the last
 * attempt to update it failed", and deriving it means the two can never
 * disagree.
 */
export function isStale<T>(snapshot: Snapshot<T>): boolean {
	return hasResult(snapshot) && snapshot.error !== null;
}

/** Spec §6.1's default cadence. */
export const POLL_INTERVAL_MS = 30_000;

export interface LiveOptions {
	/** Milliseconds between polls. `0` disables the interval (spec §6.6). */
	intervalMs?: number;
}

/**
 * One polled read.
 *
 * The read function is supplied per instance and can be swapped with
 * `retarget`, because a view's read changes whenever its filters do and a
 * second `LiveRead` per filter change would leave the first one polling.
 */
export class LiveRead<T> {
	private read: () => Promise<T>;
	private readonly notify: (snapshot: Snapshot<T>) => void;
	private readonly intervalMs: number;
	private timer: ReturnType<typeof setInterval> | null = null;
	private onFocus: (() => void) | null = null;

	/**
	 * Which read is current.
	 *
	 * ★ WITHOUT THIS, A SLOW READ OVERWRITES A FAST ONE. Poll N+1 can resolve
	 * before poll N, and a filter change fires a read while the previous
	 * filter's read is still in flight — in both cases the older result would
	 * land last and the view would show data for a filter nobody has selected.
	 * Every resolution checks its generation against this before it is allowed
	 * to touch the snapshot.
	 */
	private generation = 0;

	private state: Snapshot<T> = {
		data: null,
		error: null,
		loadedAt: null,
		loading: false,
	};

	constructor(
		read: () => Promise<T>,
		notify: (snapshot: Snapshot<T>) => void,
		options: LiveOptions = {},
	) {
		this.read = read;
		this.notify = notify;
		this.intervalMs = options.intervalMs ?? POLL_INTERVAL_MS;
	}

	get snapshot(): Snapshot<T> {
		return this.state;
	}

	/** Read once now, then keep reading on the interval and on window focus. */
	start(): void {
		this.stop();
		if (this.intervalMs > 0) {
			// Left running while the tab is hidden rather than paused on
			// `visibilitychange`: browsers already throttle background timers to
			// roughly a minute, and the focus listener below is what makes a
			// returning tab current immediately, so pausing would only add a
			// second mechanism doing the same job.
			this.timer = setInterval(() => this.poll(), this.intervalMs);
		}
		this.onFocus = () => this.poll();
		window.addEventListener("focus", this.onFocus);
		this.refresh();
	}

	/** Detach the timer and the listener. Called on teardown and before a retarget. */
	stop(): void {
		if (this.timer !== null) {
			clearInterval(this.timer);
			this.timer = null;
		}
		if (this.onFocus) {
			window.removeEventListener("focus", this.onFocus);
			this.onFocus = null;
		}
		// Anything already in flight belongs to a generation nobody will accept.
		this.generation += 1;
	}

	/**
	 * Point at a different read and start over.
	 *
	 * The snapshot is cleared, because retargeting means the filters moved and
	 * the retained data now describes a question nobody asked. That is the one
	 * case where blanking is right: it is not a failed refresh of what is on
	 * screen, it is a different screen.
	 */
	retarget(read: () => Promise<T>): void {
		this.stop();
		this.read = read;
		this.state = { data: null, error: null, loadedAt: null, loading: false };
		this.start();
	}

	/**
	 * An automatic read: the interval, and the focus handler.
	 *
	 * ★ SKIPPED WHILE ONE IS ALREADY IN FLIGHT, and that is not an
	 * optimisation. `refresh` invalidates the previous generation, so a read
	 * that consistently takes longer than the interval would be cancelled by
	 * the next tick just before it arrived — every time. The view would sit at
	 * "Reading…" forever while requests piled up behind it, and the slower the
	 * server got the more of them there would be.
	 *
	 * A person pressing Refresh, and a retarget, still go straight through:
	 * those mean "the thing I was waiting for is no longer what I want".
	 */
	private poll(): void {
		if (this.state.loading) {
			return;
		}
		this.refresh();
	}

	/** Read now, off-cycle. The manual refresh button. */
	refresh(): void {
		this.generation += 1;
		const generation = this.generation;
		this.update({ loading: true });

		this.read().then(
			(data) => {
				if (generation !== this.generation) {
					return;
				}
				this.update({
					data,
					error: null,
					loadedAt: Date.now(),
					loading: false,
				});
			},
			(cause: unknown) => {
				if (generation !== this.generation) {
					return;
				}
				// `data` and `loadedAt` are deliberately untouched: they are what
				// keeps the last good read on screen, and the view marks it stale
				// off the pair.
				this.update({
					error: cause instanceof Error ? cause : new Error(String(cause)),
					loading: false,
				});
			},
		);
	}

	private update(patch: Partial<Snapshot<T>>): void {
		this.state = { ...this.state, ...patch };
		this.notify(this.state);
	}
}
