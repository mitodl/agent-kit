import { html, render } from "lit-html";
import { beforeEach, describe, expect, it } from "vitest";
import consumersFixture from "../../fixtures/code_interface_consumers.json" with {
	type: "json",
};
import dependenciesFixture from "../../fixtures/code_repo_dependencies.json" with {
	type: "json",
};
import { DEFAULT_ROUTE, type Route } from "../route.js";
import type { InterfaceBinding, RepoDependencies } from "../types.js";
import { unwrap } from "../unwrap.js";
import {
	type BridgeCanvasHost,
	type BridgeGraph,
	bindingTable,
	bridgeView,
	contractKey,
	drillPlan,
	edgeKey,
	knowsGeneric,
	type OnSelectEdge,
	parseContractKey,
	parseEdgeKey,
	producingRows,
	stage2,
	visibleEdges,
} from "./bridge.js";
import { CanvasHost } from "./canvas-host.js";

const deps = unwrap<RepoDependencies>(
	"code_repo_dependencies",
	dependenciesFixture,
);
const route: Route = { ...DEFAULT_ROUTE, view: "bridge" };
const WEB = "https://github.com/example/web";
const INFRA = "https://github.com/example/infra";

function host(): BridgeCanvasHost {
	return new CanvasHost<BridgeGraph, OnSelectEdge>(
		async () => ({ update: () => {}, destroy: () => {} }),
		() => {},
		() => {},
	);
}

describe("visibleEdges", () => {
	it("is the tool's graph unchanged at the default filters", () => {
		// The acceptance line: the repo graph matches `code_repo_dependencies`
		// for the same scope. The server already dropped sub-0.5 consumers.
		expect(visibleEdges(deps, route)).toEqual(deps.edges);
	});

	it("drops generic contracts when asked, and an edge left with none", () => {
		const onlyGeneric: RepoDependencies = {
			repos: [WEB, INFRA],
			edges: [
				{
					consumer: WEB,
					provider: INFRA,
					weight: 1,
					kinds: { env_var: 1 },
					contracts: [
						{ kind: "env_var", key: "PORT", confidence: 1, generic: true },
					],
				},
			],
		};
		expect(visibleEdges(onlyGeneric, { ...route, hideGeneric: true })).toEqual(
			[],
		);

		const mixed = visibleEdges(deps, { ...route, hideGeneric: true });
		const infraEdge = mixed.find((edge) => edge.provider === INFRA);
		expect(infraEdge?.contracts.map((contract) => contract.key)).toEqual([
			"DATABASE_URL",
		]);
		// Recounted from what survived, not carried over from the tool.
		expect(infraEdge?.weight).toBe(1);
		expect(infraEdge?.kinds).toEqual({ env_var: 1 });
	});

	it("applies the confidence floor to each contract", () => {
		const low: RepoDependencies = {
			repos: [],
			edges: [
				{
					consumer: WEB,
					provider: INFRA,
					weight: 2,
					kinds: { endpoint: 2 },
					contracts: [
						{ kind: "endpoint", key: "/a", confidence: 0.6 },
						{ kind: "endpoint", key: "/b", confidence: 0.95 },
					],
				},
			],
		};
		const [edge] = visibleEdges(low, { ...route, confidence: 0.9 });
		expect(edge?.contracts.map((contract) => contract.key)).toEqual(["/b"]);
	});
});

describe("visibleEdges under a repo filter", () => {
	it("keeps only edges with that exact repo at an end", () => {
		// The tool's `repo` is a substring, so `.../ol-django` brings back
		// `.../ol-django-extras` too; the route's repo is a whole URI.
		const DJANGO = "https://github.com/mitodl/ol-django";
		const EXTRAS = "https://github.com/mitodl/ol-django-extras";
		const scoped: RepoDependencies = {
			repos: [DJANGO, EXTRAS, INFRA],
			edges: [
				{
					consumer: DJANGO,
					provider: INFRA,
					weight: 1,
					kinds: { env_var: 1 },
					contracts: [{ kind: "env_var", key: "A", confidence: 1 }],
				},
				{
					consumer: EXTRAS,
					provider: INFRA,
					weight: 1,
					kinds: { env_var: 1 },
					contracts: [{ kind: "env_var", key: "B", confidence: 1 }],
				},
			],
		};
		expect(
			visibleEdges(scoped, { ...route, repo: DJANGO }).map((e) => e.consumer),
		).toEqual([DJANGO]);
	});
});

describe("producingRows", () => {
	const row = unwrap<InterfaceBinding[]>(
		"code_interface_consumers",
		consumersFixture,
	)[0] as InterfaceBinding;

	it("drops endpoint consumers under the floor, which made no edge", () => {
		const rows: InterfaceBinding[] = [
			{
				...row,
				kind: "endpoint",
				role: "consumer",
				file: "kept.py",
				confidence: 0.7,
			},
			{
				...row,
				kind: "endpoint",
				role: "consumer",
				file: "phantom.py",
				confidence: 0.2,
			},
			{
				...row,
				kind: "endpoint",
				role: "provider",
				file: "served.py",
				confidence: 0.2,
			},
		];
		expect(producingRows(rows, 0.5).map((r) => r.file)).toEqual([
			"kept.py",
			"served.py",
		]);
		expect(producingRows(rows, 0.9).map((r) => r.file)).toEqual(["served.py"]);
	});

	it("reads a missing score as 1, as witan-code's own readers do", () => {
		const { confidence: _, ...old } = { ...row, kind: "endpoint" as const };
		expect(producingRows([old], 0.9)).toHaveLength(1);
	});
});

describe("knowsGeneric", () => {
	it("is false for a witan-code that predates the flag", () => {
		// Its toggle could never hide anything, so it is not offered.
		const old: RepoDependencies = {
			repos: [],
			edges: deps.edges.map((edge) => ({
				...edge,
				contracts: edge.contracts.map(({ generic: _, ...rest }) => rest),
			})),
		};
		expect(knowsGeneric(old)).toBe(false);
		expect(knowsGeneric(deps)).toBe(true);
	});
});

describe("route keys", () => {
	it("round-trip an edge and a contract whose key holds colons", () => {
		const edge = { consumer: WEB, provider: INFRA };
		expect(parseEdgeKey(edgeKey(edge))).toEqual(edge);

		const contract = { kind: "service" as const, key: `repo:${WEB}` };
		expect(parseContractKey(contractKey(contract))).toEqual(contract);
	});

	it("refuse a contract of a kind the bridge does not have", () => {
		expect(parseContractKey("database:x")).toBeNull();
		// Not an own key, so not a kind: a hand-edited URL must not send it on.
		expect(parseContractKey("toString:x")).toBeNull();
		expect(parseEdgeKey("only-one")).toBeNull();
	});
});

describe("drillPlan", () => {
	it("reads provider rows from the provider and consumer rows from the consumer", () => {
		const plan = drillPlan(
			{ consumer: WEB, provider: INFRA },
			{ kind: "env_var", key: "DATABASE_URL" },
		);
		expect(plan.providers).toEqual({
			kind: "env_var",
			key: "DATABASE_URL",
			repo: INFRA,
		});
		expect(plan.consumers?.repo).toBe(WEB);
	});

	it("reads a service edge from the deploying repo's provider binding", () => {
		const plan = drillPlan(
			{ consumer: INFRA, provider: WEB },
			{ kind: "service", key: "example/web" },
		);
		expect(plan).toEqual({
			providers: { kind: "service", key: `repo:${WEB}`, repo: INFRA },
			consumers: null,
		});
	});
});

describe("stage2", () => {
	it("keeps the resolved package fields and drops the unresolved ones", () => {
		expect(stage2("env:.:infra:main:DATABASE_URL")).toBe("infra main");
		expect(stage2("env:.:.:.:DATABASE_URL")).toBeNull();
		expect(stage2(null)).toBeNull();
	});
});

describe("bridgeView", () => {
	let root: HTMLElement;

	beforeEach(() => {
		root = document.createElement("div");
		document.body.replaceChildren(root);
	});

	function draw(patch: Partial<Route> = {}, graph: RepoDependencies = deps) {
		render(
			bridgeView({
				deps: graph,
				route: { ...route, ...patch },
				host: host(),
				drill: html``,
				onNavigate: () => {},
			}),
			root,
		);
	}

	it("lists every edge as a link, for a keyboard or a screen reader", () => {
		draw();
		const links = [...root.querySelectorAll(".bridge-edges a")];
		expect(links).toHaveLength(deps.edges.length);
		expect(
			links.map((link) => decodeURIComponent(link.getAttribute("href") ?? "")),
		).toContain(`#bridge?edge=${WEB}|${INFRA}`);
	});

	it("lists the open edge's contracts as links to their bindings", () => {
		draw({ edge: edgeKey({ consumer: WEB, provider: INFRA }) });
		const contracts = [...root.querySelectorAll(".bridge-contracts a")].map(
			(link) => link.textContent?.trim(),
		);
		expect(contracts).toEqual(["DATABASE_URL", "PORT"]);
		expect(root.querySelector(".bridge-contracts")?.textContent).toContain(
			"generic",
		);
	});

	it("says so when the open edge is filtered away, rather than drawing nothing", () => {
		const onlyPort: RepoDependencies = {
			repos: [WEB, INFRA],
			edges: [
				{
					consumer: WEB,
					provider: INFRA,
					weight: 1,
					kinds: { env_var: 1 },
					contracts: [
						{ kind: "env_var", key: "PORT", confidence: 1, generic: true },
					],
				},
			],
		};
		draw(
			{ edge: edgeKey({ consumer: WEB, provider: INFRA }), hideGeneric: true },
			onlyPort,
		);
		expect(root.querySelector(".bridge-edge")?.textContent).toContain(
			"passes the current filters",
		);
	});
});

describe("bindingTable", () => {
	it("shows where each binding is and what it resolved to", () => {
		const rows = unwrap<InterfaceBinding[]>(
			"code_interface_consumers",
			consumersFixture,
		);
		const root = document.createElement("div");
		render(bindingTable(rows), root);
		const text = root.textContent ?? "";
		expect(text).toContain("web/settings.py:40");
		expect(text).toContain("django");
		expect(text).toContain(`${WEB}#web/settings.py::DATABASES`);
	});
});
