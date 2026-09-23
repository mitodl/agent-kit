/**
 * Keeps one vis-network alive across renders, for any tab that draws one.
 *
 * Shared by the Graph and Bridge tabs, each with its own canvas module; this
 * owns only the lifecycle, so neither view has to know about the other.
 */

/**
 * What a canvas module hands back once vis-network is mounted: a network fed
 * a `G` (the graph it draws) and the id of what is selected in it.
 */
export interface Canvas<G> {
	update(graph: G, selected: string | null): void;
	destroy(): void;
}

/** Mounts a canvas into `element`, calling `onSelect` with what was clicked. */
export type MountCanvas<G, S> = (
	element: HTMLElement,
	onSelect: S,
) => Promise<Canvas<G>>;

/**
 * Keeps one vis-network alive across renders.
 *
 * lit-html re-renders the whole app on every poll, and a network rebuilt each
 * time would re-run its physics and scatter the layout every 30 seconds. So
 * the network is mounted once per container element and fed the new graph,
 * and only a new element (the tab left and came back) mounts a new one.
 *
 * `mount` is injected so the app's tests can run without a canvas: jsdom has
 * none, and vis-network draws on nothing else.
 */
export class CanvasHost<G, S> {
	private element: Element | null = null;
	private canvas: Canvas<G> | null = null;
	private pending: { graph: G; selected: string | null } | null = null;
	/** Bumped per mount, so a mount that lands after a newer one is dropped. */
	private generation = 0;
	/**
	 * Why the network could not be mounted, e.g. the lazily imported chunk
	 * 404ing because the server was upgraded under an open page. Kept until a
	 * reload: the chunk name is baked into this page's bundle, so retrying the
	 * same import cannot succeed.
	 */
	error: Error | null = null;

	constructor(
		private readonly mount: MountCanvas<G, S>,
		private readonly onSelect: S,
		private readonly onError: () => void,
	) {}

	/** The `ref` callback: called with the container, or `undefined` on removal. */
	readonly attach = (element: Element | undefined): void => {
		if (element === this.element) {
			return;
		}
		this.release();
		if (!(element instanceof HTMLElement)) {
			return;
		}
		this.element = element;
		const generation = this.generation;
		this.mount(element, this.onSelect).then(
			(canvas) => {
				if (generation !== this.generation) {
					canvas.destroy();
					return;
				}
				this.canvas = canvas;
				if (this.pending) {
					canvas.update(this.pending.graph, this.pending.selected);
				}
			},
			(error: unknown) => {
				if (generation !== this.generation) {
					return;
				}
				this.error = error instanceof Error ? error : new Error(String(error));
				this.onError();
			},
		);
	};

	show(graph: G, selected: string | null): void {
		this.pending = { graph, selected };
		this.canvas?.update(graph, selected);
	}

	/** Drop the network, e.g. when the tab is left. */
	release(): void {
		this.generation += 1;
		this.canvas?.destroy();
		this.canvas = null;
		this.element = null;
	}
}
