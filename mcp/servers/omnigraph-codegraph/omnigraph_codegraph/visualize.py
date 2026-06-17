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


def build_graph(
    rows: list[dict],
    *,
    kind: str | None = None,
    repo: str | None = None,
) -> DepGraph:
    """Compute the cross-repo dependency graph from raw binding rows.

    ``kind`` filters to one contract kind; ``repo`` keeps only edges touching a
    repo whose slug contains that substring.
    """
    groups: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"providers": set(), "consumers": set()}
    )
    for b in rows:
        if kind and b["kind"] != kind:
            continue
        role = "providers" if b["role"] == "provider" else "consumers"
        groups[(b["kind"], b["key_norm"])][role].add(b["repo"])

    graph = DepGraph()
    graph.contracts = dict(groups)
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
                    graph.edge(src, target).kinds[b_kind] += 1
            continue
        if b_kind == "service":
            continue  # image:/name: anchors aren't repo-to-repo edges
        # consumer depends on provider
        for cons in g["consumers"]:
            for prov in g["providers"]:
                if cons != prov:
                    graph.edge(cons, prov).kinds[b_kind] += 1

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
    table.add_column("depends on →", style="cyan", no_wrap=True)
    table.add_column("provider", style="green", no_wrap=True)
    table.add_column("links", justify="right")
    table.add_column("by kind")
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
    for e in graph.edges.values():
        dominant = max(e.kinds, key=e.kinds.get)
        title = ", ".join(f"{k}: {n}" for k, n in sorted(e.kinds.items()))
        edges.append(
            {
                "from": e.src,
                "to": e.dst,
                "value": e.weight,
                "title": f"{short_repo(e.src)} → {short_repo(e.dst)} ({title})",
                "color": {"color": KIND_COLORS.get(dominant, "#888")},
                "arrows": "to",
            }
        )
    legend = "".join(
        f'<span class="chip" style="background:{c}"></span>{k} '
        for k, c in KIND_COLORS.items()
    )
    html = _HTML_TEMPLATE.format(
        nodes=json.dumps(nodes),
        edges=json.dumps(edges),
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
</style></head>
<body>
  <div id="bar"><b>Cross-repo dependencies</b> — {n_repos} repos · {n_edges} links
    &nbsp;&nbsp; "A → B" = A depends on B &nbsp;&nbsp; {legend}</div>
  <div id="net"></div>
  <script>
    const nodes = new vis.DataSet({nodes});
    const edges = new vis.DataSet({edges});
    new vis.Network(document.getElementById("net"), {{nodes, edges}}, {{
      nodes: {{ color: {{ background: "#2b2f3a", border: "#5a6", highlight: {{ background: "#39415a" }} }},
               font: {{ color: "#eee" }}, borderWidth: 1 }},
      edges: {{ scaling: {{ min: 1, max: 8 }}, smooth: {{ type: "dynamic" }} }},
      physics: {{ stabilization: true, barnesHut: {{ springLength: 160 }} }},
      interaction: {{ hover: true, tooltipDelay: 100 }}
    }});
  </script>
</body></html>
"""
