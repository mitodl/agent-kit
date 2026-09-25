import { DataSet } from "vis-data";
import { type Edge, Network, type Node } from "vis-network";
import { repoLabel } from "../format.js";
import type { ContractKind } from "../types.js";
import {
	type BridgeGraph,
	edgeKey,
	KIND_COLORS,
	type OnSelectEdge,
} from "./bridge.js";
import type { Canvas } from "./canvas-host.js";

/**
 * The Bridge tab's network, loaded by `import()` on first use.
 *
 * Nodes are repos and edges are "depends on", one per repo pair, so the graph
 * is small (one node per indexed repo) and none of the Graph tab's clustering
 * applies. The CSP choices are the Graph tab's: the peer build, no vis
 * stylesheet, the tooltip styled in `style.css` (see `graph-canvas.ts`).
 */

interface VisNode extends Node {
	id: string;
}

interface VisEdge extends Edge {
	id: string;
}

/** One kind's colour for a single-kind edge; the theme's muted one otherwise. */
function edgeColor(kinds: Record<string, number>, muted: string): string {
	const present = Object.keys(kinds);
	return present.length === 1
		? (KIND_COLORS[present[0] as ContractKind] ?? muted)
		: muted;
}

export function mountBridgeCanvas(
	element: HTMLElement,
	onSelect: OnSelectEdge,
): Canvas<BridgeGraph> {
	const nodes = new DataSet<VisNode>();
	const edges = new DataSet<VisEdge>();
	const network = new Network(
		element,
		{ nodes, edges },
		{
			edges: {
				arrows: "to",
				smooth: { enabled: true, type: "continuous", roundness: 0.5 },
				font: { size: 12, strokeWidth: 0, align: "top" },
			},
			layout: { randomSeed: 1 },
			physics: {
				stabilization: { iterations: 200 },
				barnesHut: { springLength: 220, gravitationalConstant: -4000 },
			},
			interaction: {
				hover: true,
				tooltipDelay: 100,
				selectConnectedEdges: false,
			},
		},
	);

	network.on("click", (params: { nodes: string[]; edges: string[] }) => {
		// An edge under a node's label reports both; a click on the node is a
		// click on the repo, which selects nothing here.
		const [edge] = params.edges;
		if (edge && params.nodes.length === 0) {
			onSelect(edge);
		}
	});
	// Bounded, as on the Graph tab: stop once the budget is spent.
	network.on("stabilizationIterationsDone", () => {
		network.setOptions({ physics: { enabled: false } });
	});
	network.on("resize", () => {
		requestAnimationFrame(() => network.fit({ animation: false }));
	});

	return {
		update(graph: BridgeGraph, selected: string | null): void {
			const style = getComputedStyle(element);
			const fg = style.getPropertyValue("--fg").trim();
			const muted = style.getPropertyValue("--muted").trim();
			const accent = style.getPropertyValue("--accent").trim();
			const face = style.getPropertyValue("--font-ui").trim();

			const nextNodes: VisNode[] = graph.repos.map((repo) => ({
				id: repo,
				label: repoLabel(repo),
				title: repo,
				shape: "box",
				color: { background: "transparent", border: accent },
				font: { color: fg, face, size: 15 },
			}));
			const nextEdges: VisEdge[] = graph.edges.map((edge) => ({
				id: edgeKey(edge),
				from: edge.consumer,
				to: edge.provider,
				label: String(edge.weight),
				title: `${repoLabel(edge.consumer)} depends on ${repoLabel(edge.provider)}\n${edge.weight} ${edge.weight === 1 ? "contract" : "contracts"}. Click for them.`,
				width: 1 + Math.log2(edge.weight),
				color: { color: edgeColor(edge.kinds, muted) },
				font: { color: muted, face },
			}));

			const placed = new Set(nodes.getIds().map(String));
			const arriving = nextNodes.some((node) => !placed.has(node.id));
			const nodeIds = new Set(nextNodes.map((node) => node.id));
			const edgeIds = new Set(nextEdges.map((edge) => edge.id));
			nodes.remove(nodes.getIds().filter((id) => !nodeIds.has(String(id))));
			edges.remove(edges.getIds().filter((id) => !edgeIds.has(String(id))));
			nodes.update(nextNodes);
			edges.update(nextEdges);

			// vis-network bounds its layout only from `setData` or `stabilize()`;
			// rows added to the DataSet start a free-running simulation instead.
			if (arriving) {
				network.setOptions({ physics: { enabled: true } });
				network.stabilize();
			}

			network.setSelection(
				{
					nodes: [],
					edges: selected && edgeIds.has(selected) ? [selected] : [],
				},
				{ unselectAll: true, highlightEdges: false },
			);
		},
		destroy(): void {
			network.destroy();
		},
	};
}
