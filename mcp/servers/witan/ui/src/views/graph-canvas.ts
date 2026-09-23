import { DataSet } from "vis-data";
import { type Edge, Network, type Node } from "vis-network";
import {
	type Canvas,
	CLUSTER_ABOVE,
	EDGE_COLORS,
	type GraphCluster,
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

/** Prefixes a cluster key into a node id no task or project slug can have. */
const CLUSTER_PREFIX = "cluster:";

interface VisNode extends Node {
	id: string;
	/** The `GraphCluster.key`, read back by the cluster's join condition. */
	clusterKey: string;
}

interface VisEdge extends Edge {
	id: string;
}

export function mountCanvas(element: HTMLElement, onSelect: OnSelect): Canvas {
	const nodes = new DataSet<VisNode>();
	const edges = new DataSet<VisEdge>();
	const groups = new Map<string, GraphNode["group"]>();
	const clusterOf = new Map<string, string>();
	/** Cluster keys drawn collapsed right now. */
	const collapsed = new Set<string>();
	/**
	 * Cluster keys a person opened, which stay open across polls. Only ever
	 * grows: collapsing one again is a zoom out, not something to undo.
	 */
	const opened = new Set<string>();
	let fg = "";
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

	/** Lay out again, within a budget, after nodes appeared on a placed graph. */
	const restabilize = (): void => {
		network.setOptions({ physics: { enabled: true } });
		network.stabilize(RESTABILIZE_ITERATIONS);
	};

	const collapse = (cluster: GraphCluster): void => {
		const count = cluster.tasks.length;
		const summary = `${count} ${count === 1 ? "task" : "tasks"}`;
		// Typed as a `Node` because vis-network's declarations omit `id` from
		// `clusterNodeProperties`, though `cluster()` honours it
		// (`clusterNodeProperties.id` in its source), and the id is what lets a
		// click on the cluster be told apart from a click on a task.
		const id = `${CLUSTER_PREFIX}${cluster.key}`;
		const properties: Node = {
			id,
			label: `${cluster.label} · ${summary}`,
			title: `${cluster.label}\n${summary}. Click to open.`,
			shape: cluster.project ? "diamond" : "square",
			size: 22,
			color: { background: cluster.color, border: cluster.color },
			font: { color: fg, size: 15 },
		};
		network.cluster({
			joinCondition: (options: VisNode) => options.clusterKey === cluster.key,
			clusterNodeProperties: properties,
		});
		// A group of one (a project with nothing open, a repo with one
		// projectless task) is not clustered, and stays a plain node.
		if (network.isCluster(id)) {
			collapsed.add(cluster.key);
		}
	};

	const expand = (key: string): void => {
		network.openCluster(`${CLUSTER_PREFIX}${key}`);
		collapsed.delete(key);
		opened.add(key);
	};

	network.on("click", (params: { nodes: string[] }) => {
		const [id] = params.nodes;
		if (id && network.isCluster(id)) {
			expand(id.slice(CLUSTER_PREFIX.length));
			restabilize();
			return;
		}
		const group = id ? groups.get(id) : undefined;
		if (id && group) {
			onSelect(id, group);
		}
	});

	// Physics runs for a fixed stabilization budget and then stops. Left on, a
	// large graph never settled, and kept the main thread busy in 200-300 ms
	// chunks for as long as the tab was open. Stopping at the iteration budget
	// rather than at `stabilized` is what bounds it. Nodes that appear later
	// (a poll, or an opened cluster) get one more, shorter, budget.
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
			fg = style.getPropertyValue("--fg").trim();
			const muted = style.getPropertyValue("--muted").trim();

			groups.clear();
			clusterOf.clear();
			for (const node of graph.nodes) {
				groups.set(node.id, node.group);
				clusterOf.set(node.id, node.cluster);
			}
			const nextNodes: VisNode[] = graph.nodes.map((node) => ({
				id: node.id,
				label: node.label,
				title: node.tooltip,
				shape: node.group === "project" ? "diamond" : "dot",
				size: node.group === "project" ? 14 : 10,
				color: { background: node.color, border: node.color },
				font: { color: fg, size: node.group === "project" ? 14 : 13 },
				clusterKey: node.cluster,
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

			// Open every cluster before touching the data under it, and collapse
			// again afterwards: a cluster keeps the membership it was made with,
			// so a task a poll added to a collapsed project would otherwise be
			// drawn loose beside it.
			for (const key of [...collapsed]) {
				network.openCluster(`${CLUSTER_PREFIX}${key}`);
				collapsed.delete(key);
			}
			nodes.remove(nodes.getIds().filter((id) => !nodeIds.has(String(id))));
			edges.remove(edges.getIds().filter((id) => !edgeIds.has(String(id))));
			nodes.update(nextNodes);
			edges.update(nextEdges);

			let revealed = false;
			if (graph.nodes.length > CLUSTER_ABOVE) {
				// A selected node inside a cluster opens it, so a link to a task
				// shows the task rather than the project it is folded into.
				const home = selected ? clusterOf.get(selected) : undefined;
				if (home && !opened.has(home)) {
					opened.add(home);
					revealed = true;
				}
				for (const cluster of graph.clusters) {
					if (!opened.has(cluster.key)) {
						collapse(cluster);
					}
				}
			}

			if (placed.size === 0) {
				// ★ EXPLICITLY. vis-network runs its bounded initial layout only
				// from `setData`; rows added to its DataSet start a free-running
				// simulation instead, which never emits
				// `stabilizationIterationsDone` and so never stops.
				network.stabilize();
			} else if (arriving || revealed) {
				// A revealed cluster's tasks were never laid out, so they start
				// stacked on the spot the cluster was drawn at.
				restabilize();
			}

			network.selectNodes(
				selected &&
					nodeIds.has(selected) &&
					!collapsed.has(clusterOf.get(selected) ?? "")
					? [selected]
					: [],
			);
		},
		destroy(): void {
			network.destroy();
		},
	};
}
