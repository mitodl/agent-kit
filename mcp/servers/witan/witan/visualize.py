"""Workflow graph visualization.

Builds a directed graph of WorkflowProject and Task nodes with their
relationships (TaskBelongsTo, parent/child, blocks) and renders it as a
self-contained interactive HTML file or Graphviz DOT.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_COLORS = {
    "active": "#56b870",
    "completed": "#4c9be8",
    "abandoned": "#666",
}
_TASK_COLORS = {
    "open": "#e8a33d",
    "in_progress": "#6fc8e8",
    "blocked": "#e85454",
    "closed": "#555",
}
_EDGE_COLORS = {
    "belongs_to": "#444",
    "parent": "#9b7be8",
    "blocks": "#e85454",
}


@dataclass
class GraphNode:
    id: str
    label: str
    group: str  # "project" | "task"
    color: str
    status: str
    tooltip: str
    detail: dict = field(default_factory=dict)


@dataclass
class GraphEdge:
    id: int
    src: str
    dst: str
    kind: str  # "belongs_to" | "parent" | "blocks"
    label: str


@dataclass
class WorkflowGraph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)


def build_graph(
    projects: list[dict],
    tasks: list[dict],
    *,
    show_belongs_to: bool = True,
) -> WorkflowGraph:
    """Build a WorkflowGraph from project and task record dicts."""
    graph = WorkflowGraph()
    project_slugs: set[str] = set()
    task_slugs: set[str] = set()

    for p in projects:
        slug = p["slug"]
        project_slugs.add(slug)
        status = p.get("status", "active")
        color = _PROJECT_COLORS.get(status, "#56b870")
        desc = (p.get("description") or "")[:140]
        tooltip = (
            f"<b>{slug}</b><br>"
            f"phase: {p.get('phase', '?')} · status: {status}<br>"
            f"{desc}"
        )
        graph.nodes.append(
            GraphNode(
                id=slug,
                label=(p.get("title") or slug)[:40],
                group="project",
                color=color,
                status=status,
                tooltip=tooltip,
                detail=p,
            )
        )

    edge_id = 0

    for t in tasks:
        slug = t["slug"]
        task_slugs.add(slug)
        status = t.get("status", "open")
        priority = t.get("priority", "p2")
        color = _TASK_COLORS.get(status, "#e8a33d")
        desc = (t.get("description") or "")[:140]
        tooltip = f"<b>{slug}</b><br>status: {status} · priority: {priority}<br>{desc}"
        base = (t.get("title") or slug)[:35]
        label = f"[{priority}] {base}" if priority in ("p0", "p1") else base
        graph.nodes.append(
            GraphNode(
                id=slug,
                label=label,
                group="task",
                color=color,
                status=status,
                tooltip=tooltip,
                detail=t,
            )
        )

    for t in tasks:
        slug = t["slug"]
        proj_slug = t.get("project_slug")
        if show_belongs_to and proj_slug and proj_slug in project_slugs:
            graph.edges.append(
                GraphEdge(
                    id=edge_id, src=slug, dst=proj_slug, kind="belongs_to", label=""
                )
            )
            edge_id += 1
        parent_slug = t.get("parent_slug")
        if parent_slug and parent_slug in task_slugs:
            graph.edges.append(
                GraphEdge(
                    id=edge_id,
                    src=slug,
                    dst=parent_slug,
                    kind="parent",
                    label="child of",
                )
            )
            edge_id += 1
        for blocker in t.get("blocked_by") or []:
            if blocker in task_slugs:
                graph.edges.append(
                    GraphEdge(
                        id=edge_id, src=blocker, dst=slug, kind="blocks", label="blocks"
                    )
                )
                edge_id += 1

    return graph


# ── Rich terminal summary ─────────────────────────────────────────────────────


def render_rich(graph: WorkflowGraph, console=None) -> None:
    from rich.console import Console
    from rich.table import Table

    console = console or Console()

    if not graph.nodes:
        console.print("[yellow]No projects or tasks to visualize.[/]")
        return

    projects = [n for n in graph.nodes if n.group == "project"]
    tasks = [n for n in graph.nodes if n.group == "task"]
    console.print(
        f"\n[bold]Workflow graph[/bold]  "
        f"{len(projects)} projects · {len(tasks)} tasks · {len(graph.edges)} edges\n"
    )

    if projects:
        table = Table(title="Projects", header_style="bold", show_lines=False)
        table.add_column("slug", style="cyan", no_wrap=True)
        table.add_column("title")
        table.add_column("status")
        table.add_column("phase")
        for n in projects:
            d = n.detail
            table.add_row(n.id, d.get("title") or "", n.status, d.get("phase") or "")
        console.print(table)

    if tasks:
        table = Table(title="Tasks", header_style="bold", show_lines=False)
        table.add_column("slug", style="cyan", no_wrap=True)
        table.add_column("priority")
        table.add_column("status")
        table.add_column("title")
        table.add_column("project")
        for n in sorted(tasks, key=lambda x: (x.detail.get("priority") or "p9", x.id)):
            d = n.detail
            table.add_row(
                n.id,
                d.get("priority") or "",
                n.status,
                (d.get("title") or "")[:60],
                d.get("project_slug") or "",
            )
        console.print(table)


# ── Self-contained interactive HTML ───────────────────────────────────────────


def render_html(graph: WorkflowGraph, path: Path) -> Path:
    """Write a self-contained interactive HTML graph (vis-network) to path."""
    vis_nodes = []
    for n in graph.nodes:
        safe_detail = {
            k: v
            for k, v in n.detail.items()
            if isinstance(v, (str, int, float, bool, type(None), list))
        }
        vis_nodes.append(
            {
                "id": n.id,
                "label": n.label,
                "group": n.group,
                "color": {"background": n.color, "border": n.color},
                "title": n.tooltip,
                "shape": "box" if n.group == "project" else "ellipse",
                "font": {"color": "#eee"},
                "detail": safe_detail,
            }
        )

    vis_edges = []
    for e in graph.edges:
        vis_edges.append(
            {
                "id": e.id,
                "from": e.src,
                "to": e.dst,
                "label": e.label,
                "color": {"color": _EDGE_COLORS.get(e.kind, "#666")},
                "arrows": "to",
                "dashes": e.kind == "belongs_to",
            }
        )

    n_projects = sum(1 for n in graph.nodes if n.group == "project")
    n_tasks = len(graph.nodes) - n_projects
    legend = (
        '<span class="chip" style="background:#56b870"></span>project (active) '
        '<span class="chip" style="background:#4c9be8"></span>project (done) '
        '<span class="chip" style="background:#e8a33d"></span>task (open) '
        '<span class="chip" style="background:#6fc8e8"></span>in_progress '
        '<span class="chip" style="background:#e85454"></span>blocked '
        '<span class="chip" style="background:#555"></span>closed'
    )

    html = _HTML_TEMPLATE.format(
        nodes=json.dumps(vis_nodes),
        edges=json.dumps(vis_edges),
        legend=legend,
        n_projects=n_projects,
        n_tasks=n_tasks,
        n_edges=len(graph.edges),
    )
    path = path.expanduser()
    path.write_text(html)
    return path


# ── Graphviz DOT ──────────────────────────────────────────────────────────────


def render_dot(graph: WorkflowGraph, path: Path) -> Path:
    """Write a Graphviz DOT file to path."""
    lines = [
        "digraph witan_workflow {",
        "  rankdir=LR;",
        "  node [fontname=Helvetica];",
    ]
    for n in graph.nodes:
        shape = "box" if n.group == "project" else "ellipse"
        label = n.label.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        fill = n.color
        lines.append(
            f'  "{n.id}" [label="{label}", shape={shape}, '
            f'style=filled, fillcolor="{fill}", fontcolor=white];'
        )
    for e in graph.edges:
        style = "dashed" if e.kind == "belongs_to" else "solid"
        color = _EDGE_COLORS.get(e.kind, "#666")
        label = e.label.replace('"', '\\"')
        lines.append(
            f'  "{e.src}" -> "{e.dst}" [label="{label}", style={style}, color="{color}"];'
        )
    lines.append("}")
    path = path.expanduser()
    path.write_text("\n".join(lines) + "\n")
    return path


_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>witan — workflow graph</title>
<script src="https://unpkg.com/vis-network@9/standalone/umd/vis-network.min.js"></script>
<style>
  body {{ margin: 0; font: 14px system-ui, sans-serif; background: #1b1d23; color: #ddd; }}
  #bar {{ padding: 10px 16px; border-bottom: 1px solid #333; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  #net {{ width: 100vw; height: calc(100vh - 52px); }}
  .chip {{ display:inline-block; width:11px; height:11px; border-radius:2px; margin:0 4px 0 12px; vertical-align:middle; }}
  #detail {{ position: fixed; top: 62px; right: 16px; width: 380px; max-height: calc(100vh - 82px);
            overflow: auto; background: #23262f; border: 1px solid #3a3f4b; border-radius: 8px;
            padding: 12px 14px; display: none; box-shadow: 0 6px 24px rgba(0,0,0,.4); }}
  #detail h3 {{ margin: 0 0 8px; font-size: 14px; color: #eee; }}
  #detail .close {{ float: right; cursor: pointer; color: #888; font-size: 16px; }}
  #detail table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  #detail th, #detail td {{ text-align: left; padding: 3px 6px; border-bottom: 1px solid #333; vertical-align: top; }}
  #detail th {{ color: #888; font-weight: normal; white-space: nowrap; width: 110px; }}
  #detail td {{ color: #ccc; word-break: break-word; }}
</style></head>
<body>
  <div id="bar">
    <b>witan workflow graph</b> — {n_projects} projects · {n_tasks} tasks · {n_edges} edges
    &nbsp; {legend}
    &nbsp; <span style="color:#888">click a node for details</span>
  </div>
  <div id="net"></div>
  <div id="detail"></div>
  <script>
    const nodes = new vis.DataSet({nodes});
    const edges = new vis.DataSet({edges});
    const network = new vis.Network(document.getElementById("net"), {{nodes, edges}}, {{
      nodes: {{ borderWidth: 1, font: {{ color: "#eee", size: 13 }} }},
      edges: {{
        smooth: {{ type: "dynamic" }},
        font: {{ color: "#aaa", size: 11, strokeWidth: 0, align: "top" }}
      }},
      groups: {{
        project: {{ shape: "box", font: {{ size: 14, bold: true }} }},
        task:    {{ shape: "ellipse" }}
      }},
      physics: {{
        stabilization: {{ iterations: 200 }},
        barnesHut: {{ springLength: 160, gravitationalConstant: -2500 }}
      }},
      interaction: {{ hover: true, tooltipDelay: 100, navigationButtons: true }}
    }});

    const panel = document.getElementById("detail");
    function esc(s) {{
      return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));
    }}
    function showNode(id) {{
      const n = nodes.get(id);
      if (!n) return;
      const d = n.detail || {{}};
      const SKIP = new Set(["slug"]);
      const PRIORITY = ["title","status","priority","phase","type","assignee","project_slug","parent_slug","blocked_by","repos","description"];
      const keys = [...new Set([...PRIORITY, ...Object.keys(d)])].filter(k => !SKIP.has(k) && k in d);
      const rows = keys.map(k => {{
        let v = d[k];
        if (Array.isArray(v)) v = v.join(", ");
        return `<tr><th>${{esc(k)}}</th><td>${{esc(v)}}</td></tr>`;
      }}).join("");
      panel.innerHTML =
        `<span class="close" onclick="document.getElementById('detail').style.display='none'">✕</span>`
        + `<h3>${{esc(n.label)}}</h3>`
        + `<p style="color:#888;font-size:12px;margin:0 0 8px">${{esc(id)}}</p>`
        + `<table>${{rows}}</table>`;
      panel.style.display = "block";
    }}

    network.on("click", function(p) {{
      if (p.nodes.length) {{
        showNode(p.nodes[0]);
      }} else {{
        panel.style.display = "none";
      }}
    }});
  </script>
</body></html>
"""
