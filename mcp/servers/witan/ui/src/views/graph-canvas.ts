import { DataSet } from "vis-data";
import { type Edge, Network, type Node } from "vis-network";
import {
	type Canvas,
	EDGE_COLORS,
	type GraphNode,
	type OnSelect,
	type WorkflowGraph,
} from "./graph.js";

/**
 * The one module that imports vis-network, loaded by `import()` on first use.
 *
 * ★ THE PEER BUILD, AND NOT ITS STYLESHEET. The standalone build injects its
 * CSS at runtime, which `/ui/`'s CSP (`default-src 'self'`) blocks. The peer
 * build ships the CSS as a file instead, but that file is 220 KB of `data:`
 * PNGs for the navigation buttons and the manipulation UI, and the same CSP
 * blocks `data:` images. So the navigation buttons are off (the wheel zooms
 * and a drag pans), and the one element this tab uses, the tooltip, is styled
 * in `style.css`. Nothing here is fetched from anywhere but the page's origin.
 *
 * Labels sit outside the shapes (`dot`, `diamond`) in the theme's text
 * colour, rather than inside them as the CLI's dark-only page has them,
 * because no one text colour reads on every status fill in both themes.
 */

/** The budget for placing nodes a poll added, on top of an already-laid-out graph. */
const RESTABILIZE_ITERATIONS = 100;

interface VisNode extends Node {
	id: string;
}

interface VisEdge extends Edge {
	id: string;
}

export function mountCanvas(element: HTMLElement, onSelect: OnSelect): Canvas {
	const nodes = new DataSet<VisNode>();
	const edges = new DataSet<VisEdge>();
	const groups = new Map<string, GraphNode["group"]>();
	const network = new Network(
		element,
		{ nodes, edges },
		{
			edges: {
				arrows: "to",
				// Not "dynamic", which adds a hidden physics node per edge and so
				// roughly doubles what the simulation has to move.
				smooth: { enabled: true, type: "continuous", roundness: 0.5 },
				font: { size: 11, strokeWidth: 0, align: "top" },
			},
			// A fixed seed, so the same graph lays out the same way on every
			// visit instead of reshuffling each time the tab is opened.
			layout: { randomSeed: 1 },
			physics: {
				stabilization: { iterations: 200 },
				barnesHut: { springLength: 160, gravitationalConstant: -2500 },
			},
			interaction: { hover: true, tooltipDelay: 100 },
		},
	);

	network.on("click", (params: { nodes: string[] }) => {
		const [id] = params.nodes;
		const group = id ? groups.get(id) : undefined;
		if (id && group) {
			onSelect(id, group);
		}
	});

	// Physics runs for a fixed stabilization budget and then stops. Left on, a
	// graph-wide scope (about 1,150 live tasks when this was written) never
	// settled, and kept the main thread busy in 200-300 ms chunks for as long
	// as the tab was open. Stopping at the iteration budget rather than at
	// `stabilized` is what bounds it: a graph that size may never reach
	// `stabilized` at all. A poll that brings nodes the layout has not placed
	// runs one more, shorter, budget for them.
	network.on("stabilizationIterationsDone", () => {
		network.setOptions({ physics: { enabled: false } });
	});

	// The container narrows when the detail panel opens, and vis-network keeps
	// its scale across a resize, which left the graph a speck in one corner.
	// Deferred a frame because `setSize` restores the camera AFTER emitting
	// `resize`, so a fit made inside the handler is overwritten at once.
	network.on("resize", () => {
		requestAnimationFrame(() => network.fit({ animation: false }));
	});

	return {
		update(graph: WorkflowGraph, selected: string | null): void {
			// Read per update, so a theme change lands on the next poll.
			const style = getComputedStyle(element);
			const fg = style.getPropertyValue("--fg").trim();
			const muted = style.getPropertyValue("--muted").trim();

			groups.clear();
			for (const node of graph.nodes) {
				groups.set(node.id, node.group);
			}
			const nextNodes: VisNode[] = graph.nodes.map((node) => ({
				id: node.id,
				label: node.label,
				title: node.tooltip,
				shape: node.group === "project" ? "diamond" : "dot",
				size: node.group === "project" ? 14 : 10,
				color: { background: node.color, border: node.color },
				font: { color: fg, size: node.group === "project" ? 14 : 13 },
			}));
			const nextEdges: VisEdge[] = graph.edges.map((edge) => ({
				id: `${edge.kind}:${edge.src}:${edge.dst}`,
				from: edge.src,
				to: edge.dst,
				label: edge.label,
				color: { color: EDGE_COLORS[edge.kind] ?? muted },
				font: { color: muted },
				dashes: edge.kind === "belongs_to",
			}));

			// Update in place and remove what left, rather than clearing: a
			// cleared set re-runs the layout from scratch on every poll.
			const nodeIds = new Set(nextNodes.map((node) => node.id));
			const placed = new Set(nodes.getIds().map(String));
			const arriving = nextNodes.some((node) => !placed.has(node.id));
			const edgeIds = new Set(nextEdges.map((edge) => edge.id));
			nodes.remove(nodes.getIds().filter((id) => !nodeIds.has(String(id))));
			edges.remove(edges.getIds().filter((id) => !edgeIds.has(String(id))));
			nodes.update(nextNodes);
			edges.update(nextEdges);
			// Not on the first update: the network stabilizes its initial data on
			// its own, and a second budget queued behind it would only add time.
			if (arriving && placed.size > 0) {
				network.setOptions({ physics: { enabled: true } });
				network.stabilize(RESTABILIZE_ITERATIONS);
			}

			network.selectNodes(selected && nodeIds.has(selected) ? [selected] : []);
		},
		destroy(): void {
			network.destroy();
		},
	};
}
