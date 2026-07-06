"""Cross-repo dependency visualization from the bridge store.

Builds a directed "A depends on B" graph from interface bindings (A consumes a
contract B provides; for ``service`` bindings, the deploying repo depends on the
repo it deploys) and renders it as a Rich terminal summary and/or a self-contained
interactive HTML graph.
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Per-kind edge colours, reused by the Rich legend and the HTML graph.
KIND_COLORS = {
    "env_var": "#e8a33d",
    "endpoint": "#4c9be8",
    "package": "#56b870",
    "service": "#b86fd1",
}


@dataclass
class Edge:
    src: str  # depends on …
    dst: str  # … this repo
    kinds: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # the individual cross-repo linkages backing this edge: {"kind", "key"}
    contracts: list[dict] = field(default_factory=list)

    def add(self, kind: str, key: str, confidence: float = 1.0) -> None:
        self.kinds[kind] += 1
        self.contracts.append({"kind": kind, "key": key, "confidence": confidence})

    @property
    def weight(self) -> int:
        return sum(self.kinds.values())


@dataclass
class DepGraph:
    repos: set[str] = field(default_factory=set)
    edges: dict[tuple[str, str], Edge] = field(default_factory=dict)
    # (kind, key_norm) -> {"providers": {repo}, "consumers": {repo}}
    contracts: dict[tuple[str, str], dict] = field(default_factory=dict)

    def edge(self, src: str, dst: str) -> Edge:
        key = (src, dst)
        if key not in self.edges:
            self.edges[key] = Edge(src, dst)
        self.repos.update((src, dst))
        return self.edges[key]


def short_repo(repo: str) -> str:
    """``https://github.com/mitodl/mit-learn`` → ``mitodl/mit-learn``."""
    repo = repo.rstrip("/")
    parts = repo.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else repo


def cross_repo_edges(
    rows: list[dict],
    *,
    kind: str | None = None,
    min_confidence: float = 0.5,
) -> list[dict]:
    """Return the subset of binding rows that form genuine cross-repo edges.

    Filters consumer bindings by ``min_confidence`` (default 0.5).  Provider
    bindings and non-endpoint consumers are always included (their confidence
    defaults to 1.0 in the store).  Original row dicts are returned unchanged;
    the ``confidence`` key is already present on rows that have it (written by
    the indexer).  Legacy store records without a ``confidence`` key are treated
    as 1.0 during filtering but are not mutated.

    ``kind`` narrows to one contract kind if provided.
    """
    out: list[dict] = []
    for row in rows:
        if kind and row.get("kind") != kind:
            continue
        raw_conf = row.get("confidence")
        conf = float(raw_conf if raw_conf is not None else 1.0)
        if row.get("role") == "consumer" and row.get("kind") == "endpoint":
            if conf < min_confidence:
                continue
        out.append(row)
    return out


def build_graph(
    rows: list[dict],
    *,
    kind: str | None = None,
    repo: str | None = None,
    min_confidence: float = 0.5,
    min_precision: str = "heuristic",
    repo_symbol_rows: list[dict] | None = None,
) -> DepGraph:
    """Compute the cross-repo dependency graph from raw binding rows.

    ``kind`` filters to one contract kind; ``repo`` keeps only edges touching a
    repo whose slug contains that substring.  ``min_confidence`` (default 0.5)
    suppresses low-confidence endpoint consumer rows before graph construction —
    only endpoint consumers are filtered; all providers and non-endpoint consumers
    pass through unconditionally.

    ``min_precision`` (docs/EDGE_PRECISION_TIERS.md) defaults to ``"heuristic"``
    — every edge this function has always produced, unchanged. Pass
    ``"precise"`` plus ``repo_symbol_rows`` (a full ``all_repo_symbols`` dump)
    to keep only edges also covered by a Stage-2 canonical-symbol join;
    ``"fuzzy"`` is currently identical to ``"heuristic"`` (no fuzzy tier
    exists yet). The special ``service`` "repo depends on what it deploys"
    edge is unaffected by ``min_precision`` — it isn't a symbol-joined
    consumer/provider relationship. Raises ``ValueError`` for any other
    value, matching ``edges.cross_repo_edges``.
    """
    from . import edges as edges_module

    if min_precision not in edges_module.PRECISION_TIERS:
        raise ValueError(
            f"min_precision must be one of {edges_module.PRECISION_TIERS!r}"
        )

    filtered = cross_repo_edges(rows, kind=kind, min_confidence=min_confidence)

    require_precise = min_precision == "precise"
    precise_pairs = None
    if require_precise:
        precise_pairs = edges_module.precise_pairs(repo_symbol_rows or [])

    groups: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"providers": set(), "consumers": {}}
    )
    # consumers maps repo -> confidence for the best (highest) confidence row
    # for each (kind, key_norm, consumer_repo) triple.
    consumer_conf: dict[tuple[str, str, str], float] = {}

    for b in filtered:
        b_kind = b["kind"]
        key_norm = b["key_norm"]
        b_repo = b["repo"]
        if b["role"] == "provider":
            groups[(b_kind, key_norm)]["providers"].add(b_repo)
        else:
            groups[(b_kind, key_norm)]["consumers"][b_repo] = None  # placeholder
            ck = (b_kind, key_norm, b_repo)
            raw_conf = b.get("confidence")
            conf = float(raw_conf if raw_conf is not None else 1.0)
            if ck not in consumer_conf or conf > consumer_conf[ck]:
                consumer_conf[ck] = conf

    graph = DepGraph()
    graph.contracts = {k: dict(v) for k, v in groups.items()}
    for (b_kind, key_norm), g in groups.items():
        for cons in g["consumers"]:
            graph.repos.add(cons)
        for prov in g["providers"]:
            graph.repos.add(prov)

        if b_kind == "service" and key_norm.startswith("repo:"):
            # The deploying repo (provider) depends on the repo it deploys.
            target = key_norm[len("repo:") :]
            for src in g["providers"]:
                if src != target:
                    graph.edge(src, target).add(b_kind, short_repo(target))
            continue
        if b_kind == "service":
            continue  # image:/name: anchors aren't repo-to-repo edges
        # consumer depends on provider; skip when the consumer repo itself also
        # provides this key_norm (the repo self-serves its own route, so the
        # path-key collision with a foreign provider must not generate an edge).
        self_providing = g["providers"]
        for cons in g["consumers"]:
            if cons in self_providing:
                continue
            for prov in g["providers"]:
                if cons == prov:
                    continue
                if (
                    require_precise
                    and (cons, prov, b_kind, key_norm) not in precise_pairs
                ):
                    continue
                conf = consumer_conf.get((b_kind, key_norm, cons), 1.0)
                graph.edge(cons, prov).add(b_kind, key_norm, conf)

    if repo:
        graph.edges = {
            k: e for k, e in graph.edges.items() if repo in e.src or repo in e.dst
        }
        graph.repos = {r for e in graph.edges.values() for r in (e.src, e.dst)} or {
            r for r in graph.repos if repo in r
        }
    return graph


# ── Rich terminal summary ─────────────────────────────────────────


def render_rich(graph: DepGraph, console=None) -> None:
    from rich.console import Console
    from rich.table import Table

    console = console or Console()

    if not graph.edges:
        console.print(
            "[yellow]No cross-repo dependencies found.[/] "
            "Index at least two related repos first."
        )
        return

    kind_totals: dict[str, int] = defaultdict(int)
    for e in graph.edges.values():
        for k, n in e.kinds.items():
            kind_totals[k] += n

    legend = "  ".join(
        f"[{_rich_color(k)}]●[/] {k} ({kind_totals[k]})"
        for k in KIND_COLORS
        if kind_totals.get(k)
    )
    console.print(
        f"\n[bold]Cross-repo dependencies[/]  "
        f"{len(graph.repos)} repos · {len(graph.edges)} links\n{legend}\n"
    )

    table = Table(show_lines=False, header_style="bold")
    table.add_column("depends on →", style="cyan", overflow="fold", no_wrap=False)
    table.add_column("provider", style="green", overflow="fold", no_wrap=False)
    table.add_column("links", justify="right", no_wrap=True)
    table.add_column("by kind", overflow="fold", no_wrap=False)
    for e in sorted(graph.edges.values(), key=lambda e: e.weight, reverse=True):
        kinds = "  ".join(
            f"[{_rich_color(k)}]{k}:{n}[/]" for k, n in sorted(e.kinds.items())
        )
        table.add_row(short_repo(e.src), short_repo(e.dst), str(e.weight), kinds)
    console.print(table)


def _rich_color(kind: str) -> str:
    return {
        "env_var": "yellow",
        "endpoint": "blue",
        "package": "green",
        "service": "magenta",
    }.get(kind, "white")


# ── Self-contained interactive HTML ───────────────────────────────


def render_html(graph: DepGraph, path: Path) -> Path:
    """Write an interactive force-directed graph (vis-network) to ``path``."""
    nodes = [
        {"id": r, "label": short_repo(r), "shape": "box"} for r in sorted(graph.repos)
    ]
    edges = []
    for i, e in enumerate(graph.edges.values()):
        dominant = max(e.kinds, key=e.kinds.get)
        title = ", ".join(f"{k}: {n}" for k, n in sorted(e.kinds.items()))
        edges.append(
            {
                "id": i,
                "from": e.src,
                "to": e.dst,
                "value": e.weight,
                "label": f"{short_repo(e.src)} → {short_repo(e.dst)}",
                "title": f"{short_repo(e.src)} → {short_repo(e.dst)} ({title}) — click for details",
                "color": {"color": KIND_COLORS.get(dominant, "#888")},
                "arrows": "to",
                "contracts": sorted(e.contracts, key=lambda c: (c["kind"], c["key"])),
            }
        )
    legend = "".join(
        f'<span class="chip" style="background:{c}"></span>{k} '
        for k, c in KIND_COLORS.items()
    )
    html = _HTML_TEMPLATE.format(
        nodes=json.dumps(nodes),
        edges=json.dumps(edges),
        kind_colors=json.dumps(KIND_COLORS),
        legend=legend,
        n_repos=len(graph.repos),
        n_edges=len(graph.edges),
    )
    path = path.expanduser()
    path.write_text(html)
    return path


_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Cross-repo dependencies</title>
<script src="https://unpkg.com/vis-network@9/standalone/umd/vis-network.min.js"></script>
<style>
  body {{ margin: 0; font: 14px system-ui, sans-serif; background: #1b1d23; color: #ddd; }}
  #bar {{ padding: 10px 16px; border-bottom: 1px solid #333; }}
  #net {{ width: 100vw; height: calc(100vh - 50px); }}
  .chip {{ display:inline-block; width:11px; height:11px; border-radius:2px; margin:0 4px 0 12px; vertical-align:middle; }}
  #detail {{ position: fixed; top: 60px; right: 16px; width: 420px; max-height: calc(100vh - 80px);
            overflow: auto; background: #23262f; border: 1px solid #3a3f4b; border-radius: 8px;
            padding: 12px 14px; display: none; box-shadow: 0 6px 24px rgba(0,0,0,.4); }}
  #detail h3 {{ margin: 0 0 8px; font-size: 14px; }}
  #detail .close {{ float: right; cursor: pointer; color: #888; }}
  #detail table {{ border-collapse: collapse; width: 100%; }}
  #detail th, #detail td {{ text-align: left; padding: 4px 8px; border-bottom: 1px solid #333;
            font-size: 13px; word-break: break-all; }}
  #detail .kind {{ white-space: nowrap; font-weight: 600; }}
</style></head>
<body>
  <div id="bar"><b>Cross-repo dependencies</b> — {n_repos} repos · {n_edges} links
    &nbsp;&nbsp; "A → B" = A depends on B &nbsp;&nbsp; {legend}
    &nbsp;&nbsp; <span style="color:#888">click an edge for the linkage list</span></div>
  <div id="net"></div>
  <div id="detail"></div>
  <script>
    const KIND_COLORS = {kind_colors};
    const nodes = new vis.DataSet({nodes});
    const edges = new vis.DataSet({edges});
    const network = new vis.Network(document.getElementById("net"), {{nodes, edges}}, {{
      nodes: {{ color: {{ background: "#2b2f3a", border: "#5a6", highlight: {{ background: "#39415a" }} }},
               font: {{ color: "#eee" }}, borderWidth: 1 }},
      edges: {{ scaling: {{ min: 1, max: 8 }}, smooth: {{ type: "dynamic" }},
               font: {{ color: "#aaa", size: 11, strokeWidth: 0, align: "top" }} }},
      physics: {{ stabilization: true, barnesHut: {{ springLength: 160 }} }},
      interaction: {{ hover: true, tooltipDelay: 100 }}
    }});

    const panel = document.getElementById("detail");
    function esc(s) {{ return String(s).replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c])); }}
    function showEdge(id) {{
      const e = edges.get(id);
      if (!e) return;
      const rows = e.contracts.map(c =>
        `<tr><td class="kind" style="color:${{KIND_COLORS[c.kind] || '#ccc'}}">${{esc(c.kind)}}</td>`
        + `<td>${{esc(c.key)}}</td></tr>`).join("");
      panel.innerHTML = `<span class="close" onclick="document.getElementById('detail').style.display='none'">✕</span>`
        + `<h3>${{esc(e.label)}}</h3>`
        + `<table><thead><tr><th>kind</th><th>contract</th></tr></thead><tbody>${{rows}}</tbody></table>`;
      panel.style.display = "block";
    }}
    network.on("click", p => p.edges.length ? showEdge(p.edges[0]) : (panel.style.display = "none"));
  </script>
</body></html>
"""
