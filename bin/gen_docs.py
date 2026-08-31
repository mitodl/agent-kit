#!/usr/bin/env -S uv run --quiet --all-packages --python 3.12 python
"""Generate the reference half of the witan-context docs site from live sources.

WHY. A reference page that is written by hand is a reference page that is wrong
by the second release. Every page this script emits is derived from the thing it
documents — the registered FastMCP tool objects, the cyclopts command tree, the
``.pg`` schema files — so the only way for it to drift is for someone to change
the code and not re-run the generator. ``--check`` is what makes that a CI
failure rather than a slow rot.

WHAT IS *NOT* GENERATED. Prose. Tutorials, how-to guides, and explanation are
written by hand under ``docs/guides/`` and ``docs/explanation/`` and this script
never touches them. The line is deliberate: a generator can state that
``memory_store`` takes a ``kind`` parameter, but only a person can say when you
should reach for it.

THE ONE HYBRID IS THE ENVIRONMENT REFERENCE. Env var *names* are discoverable
from source; what they mean is not. So the names are discovered here and the
descriptions live in ``docs/_data/environment.toml``, and a name with no entry
there is a hard error. That way a newly-added env var cannot ship undocumented,
but the description is still written by someone who knows what it does.

THE INTERPRETER IS PINNED IN THE SHEBANG, AND HAS TO BE. The JSON Schema
FastMCP derives from a ``Literal`` does not order its ``enum`` the same way on
every Python: ``BindingKind`` comes out as
``env_var, package, service, endpoint`` on 3.14 and
``env_var, endpoint, package, service`` on 3.12. Nothing here is
hash-dependent — the order is stable within a version and differs between them
— so without a pin, generating on one Python and checking on another reports
pages as stale that nobody edited. That is exactly what happened: CI resolved
3.12, the author's machine had 3.14, and ``--check`` failed on two pages with
no change behind them. Pinning makes every contributor and CI agree; the
version itself is arbitrary, it only has to be fixed.

Usage:
    ./bin/gen_docs.py            # regenerate everything
    ./bin/gen_docs.py --check    # fail if anything is stale (CI)
    ./bin/gen_docs.py mcp-tools  # regenerate one section
"""

from __future__ import annotations

import ast
import asyncio
import difflib
import itertools
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

import cyclopts

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
REFERENCE = DOCS / "reference"
DATA = DOCS / "_data"

# Importing the servers constructs an OmnigraphClient at module scope and
# *creates the store directory* on the way. Point that at a throwaway rather
# than at the real graph: generation only reads registered tool metadata, and it
# must never be able to touch — or lazily initialise — someone's actual store.
# A non-writable path is not an option; `_ensure_graph` would raise on the mkdir.
_DOC_STORE = Path(tempfile.gettempdir()) / "witan-docs-generation" / "graph.omni"
os.environ["WITAN_MEMORY_URI"] = str(_DOC_STORE)

# ★ AND THE STORE HAS TO ALREADY EXIST, OR THIS NEEDS THE OMNIGRAPH BINARY.
# `witan.server._ensure_graph` branches on it: an existing store tolerates a
# missing binary (it catches the RuntimeError and returns), while a missing one
# must be `omnigraph init`-ed and therefore cannot. Generating documentation has
# no business requiring a downloaded binary — it reads tool metadata and never
# opens a graph — and requiring one coupled the docs CI job to the binary's
# availability. That bill came due immediately: the upstream `edge` tag moved,
# the pinned checksum stopped matching, the install step failed, and a docs-only
# change went red for a reason that had nothing to do with docs.
#
# Creating the directory is enough to take the tolerant branch. Nothing ever
# reads or writes it.
_DOC_STORE.mkdir(parents=True, exist_ok=True)


def _ensure_omnigraph_on_path() -> None:
    """Put a no-op ``omnigraph`` on PATH when no real one is installed.

    ``OmnigraphClient.__init__`` resolves the binary eagerly, and both servers
    construct a module-level client, so importing them needs *something* named
    ``omnigraph`` even though generation never opens a graph. On a machine with
    witan set up this does nothing — the real binary is found first and
    behaviour is identical to before. It only fires in a bare environment such
    as the docs CI job, which has no reason to download a 12MB binary to read
    docstrings.

    The stub is a no-op rather than an error: with the store pre-created above,
    the only thing that runs it is ``schema_apply_if_changed``, which is
    comparing a schema against a store nothing will ever read.
    """
    if shutil.which("omnigraph") or (Path.home() / ".local/bin/omnigraph").exists():
        return
    stub_dir = Path(tempfile.gettempdir()) / "witan-docs-generation" / "bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "omnigraph"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    os.environ["PATH"] = f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}"


_ensure_omnigraph_on_path()

BANNER = """<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: {source}
-->
"""

app = cyclopts.App(
    name="gen-docs",
    help="Generate the witan-context reference documentation.",
)

# ── Shared state ────────────────────────────────────────────────────
#
# `--check` turns every write into a comparison. Collected rather than raised on
# the spot so one run reports every stale page, not just the first.
_check_mode = False
_stale: list[str] = []


def emit(path: Path, content: str) -> None:
    """Write ``content`` to ``path``, or record it as stale under ``--check``.

    ★ TRAILING WHITESPACE IS STRIPPED BECAUSE THE PRE-COMMIT HOOK STRIPS IT.
    Without this the two fight forever: the generator writes a line with a
    trailing space (cyclopts' Markdown does, in the CLI reference), the
    `trailing-whitespace` hook removes it on commit, and the next `docs-check`
    regenerates the space and declares the committed page stale. Emitting what
    the hook would accept is the only stable fixed point.
    """
    content = "\n".join(line.rstrip() for line in content.splitlines())
    if not content.endswith("\n"):
        content += "\n"
    rel = path.relative_to(REPO_ROOT)
    if _check_mode:
        current = path.read_text() if path.exists() else ""
        if current != content:
            _stale.append(str(rel))
            diff = difflib.unified_diff(
                current.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"{rel} (committed)",
                tofile=f"{rel} (regenerated)",
                n=1,
            )
            sys.stderr.write("".join(list(diff)[:40]))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"  wrote {rel}")


# ── JSON Schema rendering ───────────────────────────────────────────


def type_str(schema: dict[str, Any]) -> str:
    """Render a JSON Schema fragment as a short, readable type.

    Optional parameters arrive as ``anyOf: [T, null]`` — the ``null`` branch is
    dropped and the result marked with ``?``, because "string or null" is a
    Pydantic implementation detail and "optional string" is what a reader wants.
    """
    if "enum" in schema:
        return " \\| ".join(f"`{v}`" for v in schema["enum"])
    if "anyOf" in schema:
        branches = [b for b in schema["anyOf"] if b.get("type") != "null"]
        nullable = len(branches) != len(schema["anyOf"])
        rendered = " \\| ".join(type_str(b) for b in branches)
        return f"{rendered}?" if nullable else rendered
    kind = schema.get("type")
    if kind == "array":
        return f"list[{type_str(schema.get('items', {}))}]"
    if kind == "object":
        return "object"
    return {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
    }.get(kind, kind or "any")


def default_str(schema: dict[str, Any], required: bool) -> str:
    if required:
        return "**required**"
    if "default" not in schema:
        return "—"
    value = schema["default"]
    if value is None:
        return "`null`"
    if value == []:
        return "`[]`"
    return f"`{value!r}`" if isinstance(value, str) else f"`{value}`"


def clean_desc(text: str | None) -> str:
    """Flatten a schema description into one Markdown table cell.

    Newlines become ``<br>`` so multi-line parameter docs survive a table, and
    the double-backtick spans the docstrings use are already valid Markdown code
    spans, so they are left alone.

    ★ PIPES MUST BE ESCAPED, AND THE FAILURE IS SILENT. A description that
    enumerates its options — "``bug`` | ``feature`` | ``task``" — carries raw
    ``|``, which the table parser reads as column separators. The row does not
    visibly break: the cells past the fourth are simply DISCARDED, so
    ``task_update``'s ``status`` rendered as the single word "open" and its
    warning about closing a task unblocking its dependents vanished from the
    page. ``type_str`` already escapes for the same reason.
    """
    if not text:
        # An empty cell reads as a broken table rather than as a missing
        # docstring. `gen_mcp_tools` reports the real count separately.
        return "—"
    text = text.replace("|", "\\|")
    return "<br>".join(
        line.strip() for line in text.strip().splitlines() if line.strip()
    )


# ── MCP tool reference ──────────────────────────────────────────────

# Which page each tool lands on, and the order the pages are listed in. Grouped
# by the concept a reader is holding in their head ("I want to record something"
# / "I want to coordinate work"), not by module or prefix — `recall` and
# `topic_get` belong with memory even though they share no prefix with it.
TOOL_GROUPS: list[tuple[str, str, str, list[str]]] = [
    (
        "memory",
        "Memory & recall",
        (
            "Recording durable knowledge in the shared graph, and reading it back. "
            "`recall` is the default read — it composes BM25 search, graph expansion, "
            "superseded-pruning, and re-ranking into one call. The narrower reads below "
            "exist for when you already know exactly what you want."
        ),
        [
            "recall",
            "memory_store",
            "memory_get",
            "memory_update",
            "memory_delete",
            "memory_list",
            "memory_search",
            "memory_link",
            "memory_neighbors",
            "memory_symbols",
            "memory_for_contract",
            "symbol_context",
            "topic_get",
            "store_merge",
            # The repair half of `store_merge`: it restamps rows a migration
            # already landed under a local identity, which `store_merge`'s own
            # `claim_from_author` can only do on arrival.
            "claim_authorship",
        ],
    ),
    (
        "tasks",
        "Tasks",
        (
            "The work-coordination layer: what needs doing, what blocks what, and who "
            "holds which piece of work right now. `task_claim` is a **best-effort** "
            "compare-and-swap — it detects and rejects most lost races, but it is an "
            "advisory lease, not a hard lock. See "
            "[ADR 0003](../../explanation/decisions/0003-atomic-task-claims-cas.md) for "
            "what is and is not guaranteed."
        ),
        [
            "task_create",
            "task_get",
            "task_list",
            "task_ready",
            "task_update",
            "task_claim",
            "task_release",
            "task_close",
            "task_comment",
            "task_link",
            "task_unlink",
            "task_for_branch",
        ],
    ),
    (
        "workflow",
        "Workflow projects & sessions",
        (
            "Tracking an engineering objective across many agent sessions without an "
            "explicit hand-off. A project spans phases and repos; each session links "
            "itself to one, and a completed project leaves a trace behind for later "
            "pattern-mining."
        ),
        [
            "workflow_project_create",
            "workflow_project_get",
            "workflow_project_status",
            "workflow_project_list",
            "workflow_project_update",
            "workflow_project_advance",
            "workflow_project_complete",
            "workflow_project_block",
            "workflow_project_unblock",
            "workflow_project_get_blockers",
            "workflow_project_link_memory",
            "workflow_project_memories",
            "workflow_session_start",
            "workflow_session_end",
            "workflow_session_list",
            "workflow_trace_list",
            "workflow_trace_get",
            "workflow_trace_annotate",
            "workflow_trace_mine",
        ],
    ),
    (
        "code",
        "Code graph",
        (
            "Exact symbol lookups, caller graphs, change-impact analysis, and cross-repo "
            "contract tracing, served from a tree-sitter index. Reach for these instead of "
            "grep when you need a definition, a blast radius, or the provider of a shared "
            "env var, endpoint, package, or service."
        ),
        [],  # filled with every code_* tool, in registration order
    ),
]


async def _collect_tools() -> dict[str, Any]:
    import witan.server as witan_server
    import witan_code.server as code_server

    tools = {}
    for mcp in (witan_server.mcp, code_server.mcp):
        for tool in await mcp._list_tools():
            tools[tool.name] = tool
    return tools


def render_tool(tool: Any) -> str:
    out = [f"## `{tool.name}`\n"]
    if tool.description:
        out.append(tool.description.strip() + "\n")

    params = tool.parameters or {}
    props: dict[str, Any] = params.get("properties", {})
    required = set(params.get("required", []))
    if props:
        out.append("| Parameter | Type | Default | Description |")
        out.append("| --- | --- | --- | --- |")
        # Required parameters first: that is the order you have to supply them
        # in mentally, and it puts the ten optional filters below the fold.
        ordered = sorted(props.items(), key=lambda kv: kv[0] not in required)
        for name, schema in ordered:
            out.append(
                f"| `{name}` | {type_str(schema)} | {default_str(schema, name in required)} "
                f"| {clean_desc(schema.get('description'))} |"
            )
        out.append("")
    else:
        out.append("*Takes no parameters.*\n")
    return "\n".join(out)


@app.command(name="mcp-tools")
def gen_mcp_tools() -> None:
    """Generate the MCP tool reference from the registered FastMCP tools."""
    tools = asyncio.run(_collect_tools())

    # The code group is declared empty and filled here so a new `code_*` tool is
    # picked up without editing this file; the other three are listed explicitly
    # because their reading order is a deliberate choice, not alphabetical.
    code_tools = sorted(n for n in tools if n.startswith("code_"))
    groups = [
        (slug, title, blurb, names or code_tools)
        for slug, title, blurb, names in TOOL_GROUPS
    ]

    assigned = {name for _, _, _, names in groups for name in names}
    if unassigned := sorted(set(tools) - assigned):
        raise SystemExit(
            f"Tool(s) not assigned to any documentation group: {unassigned}\n"
            f"Add them to TOOL_GROUPS in {Path(__file__).name}."
        )

    for slug, title, blurb, names in groups:
        missing = [n for n in names if n not in tools]
        if missing:
            raise SystemExit(f"Group {slug!r} lists unregistered tool(s): {missing}")
        body = [
            BANNER.format(source="the registered FastMCP tool objects"),
            f"# {title}\n",
            blurb + "\n",
        ]
        body.extend(render_tool(tools[n]) for n in names)
        emit(REFERENCE / "mcp-tools" / f"{slug}.md", "\n".join(body))

    _emit_tool_index(groups, tools)
    _report_param_coverage(tools)


def _report_param_coverage(tools: dict) -> None:
    """Print how many tool parameters ship with no description.

    Not a failure — several of these tools predate the convention and the
    reference is still useful without them. But an undescribed parameter is not
    only a hole in this page: FastMCP sends the same schema to the model, so the
    agent calling the tool is working blind too. Printing the count keeps that
    visible rather than letting 62 empty cells look like a rendering bug.
    """
    total = missing = 0
    worst: list[tuple[int, str]] = []
    for name, tool in tools.items():
        props = (tool.parameters or {}).get("properties", {})
        gaps = sum(1 for schema in props.values() if not schema.get("description"))
        total += len(props)
        missing += gaps
        if gaps:
            worst.append((gaps, name))
    if not missing:
        return
    top = ", ".join(f"{n} ({c})" for c, n in sorted(worst, reverse=True)[:5])
    print(
        f"  note: {missing}/{total} tool parameters have no description "
        f"({len(worst)}/{len(tools)} tools). Worst: {top}"
    )


def _emit_tool_index(groups: list, tools: dict) -> None:
    body = [
        BANNER.format(source="the registered FastMCP tool objects"),
        "# MCP tools\n",
        (
            f"witan exposes **{len(tools)} MCP tools** across four domains. A single "
            "`witan serve` mounts all of them, so one MCP entry in your agent's config "
            "gets you the whole surface.\n"
        ),
        "| Domain | Tools | What it covers |",
        "| --- | --- | --- |",
    ]
    for slug, title, blurb, names in groups:
        # First sentence only — the full blurb is on the page itself.
        summary = blurb.split(". ")[0].rstrip(".") + "."
        body.append(f"| [{title}]({slug}.md) | {len(names)} | {summary} |")
    body.append("")
    emit(REFERENCE / "mcp-tools" / "index.md", "\n".join(body))


# ── CLI reference ───────────────────────────────────────────────────


@app.command(name="cli")
def gen_cli() -> None:
    """Generate the CLI reference from the cyclopts command tree."""
    from witan.cli import app as witan_app

    # `witan code …` is mounted into the umbrella app when witan-code is
    # installed, so a recursive render of the umbrella already contains the
    # code-graph CLI. Rendering it once, from the top, is what a reader
    # actually types.
    markdown = witan_app.generate_docs(
        output_format="markdown",
        recursive=True,
        heading_level=1,
    )
    emit(
        REFERENCE / "cli.md",
        BANNER.format(source="the cyclopts command tree (`witan.cli.app`)") + markdown,
    )


# ── Graph schema reference ──────────────────────────────────────────

_DIVIDER_RE = re.compile(r"[─\-=_]{3,}.*|.*[─\-=]{6,}\s*")
_NODE_RE = re.compile(r"^node\s+(\w+)\s*\{")
_EDGE_RE = re.compile(r"^edge\s+(\w+)\s*:\s*(\w+)\s*->\s*(\w+)")
_FIELD_RE = re.compile(r"^\s*(\w+)\s*:\s*(.+?)\s*(?://\s*(.*))?$")


def parse_pg(path: Path) -> tuple[list[dict], list[dict]]:
    """Parse an omnigraph ``.pg`` schema into node and edge records.

    Comment lines immediately above a declaration are its documentation — the
    convention the schema files already follow — so the prose written next to
    the schema is what ends up on the page, with no second copy to maintain.
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    pending: list[str] = []
    current: dict | None = None

    for raw in path.read_text().splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("//"):
            text = stripped[2:].strip()
            # Section dividers (`── Workflow Tracking ──`) are layout, not
            # documentation, and would otherwise open every node's description.
            if not _DIVIDER_RE.fullmatch(text):
                pending.append(text)
            continue
        if not stripped:
            # A blank line does NOT break the comment→declaration association.
            # Every doc block in these schema files is separated from the thing
            # it documents by exactly one blank line, so treating a blank as a
            # reset silently drops the documentation for every node and edge —
            # which is what the first version of this parser did.
            continue

        if current is not None:
            if stripped.startswith("}"):
                current = None
                pending = []
                continue
            match = _FIELD_RE.match(line)
            if match:
                name, type_spec, comment = match.groups()
                current["fields"].append(
                    {
                        "name": name,
                        "type": type_spec.rstrip(","),
                        "comment": comment or "",
                    }
                )
            continue

        if match := _NODE_RE.match(stripped):
            current = {"name": match.group(1), "doc": pending, "fields": []}
            nodes.append(current)
            pending = []
            continue

        if match := _EDGE_RE.match(stripped):
            name, src, dst = match.groups()
            edges.append({"name": name, "from": src, "to": dst, "doc": pending})
            pending = []
            continue

        pending = []

    return nodes, edges


def _schema_doc(lines: list[str]) -> str:
    """Join a declaration's comment block into renderable Markdown.

    Angle brackets are escaped: these files describe slugs as
    ``wp-<sanitised-title>-<6hex>``, and an unescaped ``<sanitised-title>`` is
    parsed as an HTML tag and vanishes from the page.
    """
    text = "\n".join(lines).strip()
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _render_schema(title: str, intro: str, source: Path) -> str:
    nodes, edges = parse_pg(source)
    rel = source.relative_to(REPO_ROOT)
    body = [
        BANNER.format(source=rel),
        f"# {title}\n",
        intro + "\n",
        f"Source of truth: [`{rel}`](https://github.com/mitodl/agent-kit/blob/main/{rel}).\n",
        "## Nodes\n",
    ]
    for node in nodes:
        body.append(f"### `{node['name']}`\n")
        if doc := _schema_doc(node["doc"]):
            body.append(doc + "\n")
        body.append("| Field | Type | Notes |")
        body.append("| --- | --- | --- |")
        for field in node["fields"]:
            body.append(
                f"| `{field['name']}` | `{field['type']}` | {field['comment']} |"
            )
        body.append("")

    body.append("## Edges\n")
    body.append(
        "Edges are directional and typed. A traversal names the edge in lowercase "
        "(`supersedes`, `blocks`), while the schema declares it in PascalCase.\n"
    )
    body.append("| Edge | From | To | Meaning |")
    body.append("| --- | --- | --- | --- |")
    for edge in edges:
        doc = _schema_doc(edge["doc"]).replace("\n", " ")
        body.append(f"| `{edge['name']}` | `{edge['from']}` | `{edge['to']}` | {doc} |")
    body.append("")
    return "\n".join(body)


@app.command(name="schema")
def gen_schema() -> None:
    """Generate the graph schema reference from the `.pg` files."""
    emit(
        REFERENCE / "graph-schema.md",
        _render_schema(
            "Graph schema",
            "The shape of the witan graph: what a memory, a task, a project, and a "
            "session are, and how they connect. Every MCP tool is ultimately a read "
            "or a write against these types.",
            REPO_ROOT / "mcp/servers/witan/schema/schema.pg",
        ),
    )
    emit(
        REFERENCE / "bridge-schema.md",
        _render_schema(
            "Cross-repo bridge schema",
            "The bridge store links repositories to each other by shared contract "
            "keys — an env var, an HTTP endpoint, a package name, a service name. It "
            "is what makes `code_interface_providers` and `code_cross_repo_impact` "
            "able to answer a question that spans two checkouts.",
            REPO_ROOT / "mcp/servers/witan-code/witan_code/schema/bridge-schema.pg",
        ),
    )


# ── Environment variable reference ──────────────────────────────────

# Anything matching these is a name fragment or a test fixture, not a real
# setting. `WITAN_SCAN_` and friends appear as f-string prefixes in code that
# builds a var name dynamically; the concrete vars they build are listed
# individually in the data file.
_ENV_SKIP = re.compile(r"^WITAN_(TEST_|CODE_TEST_)|_$")
# Digits are part of a name, not a boundary: without them `WITAN_RANK_W_BM25`
# silently truncates to `WITAN_RANK_W_BM` and the docs describe a var that does
# not exist. The left boundary matters just as much — without it the private
# Python constants `_WITAN_ARGS` and `_WITAN_CODE_ARGS` are read as env vars and
# the reference grows two settings nothing has ever honoured.
_ENV_RE = re.compile(r"(?<![A-Za-z0-9_])WITAN_[A-Z0-9_]+")


def _py_env_names(source: str) -> set[str]:
    """``WITAN_*`` names a Python module actually *references*, not merely mentions.

    ★ SCANNING RAW TEXT HERE PRODUCES SETTINGS THAT DO NOT EXIST. Three showed up
    the first time this ran:

      ``WITAN_BRANCH``          appears only in a comment saying there is *no*
                                such override
      ``WITAN_EMBED_ENABLED``   named in two docstrings; embeddings are deferred
                                and nothing reads it
      ``WITAN_REQUEST_TIMEOUT`` a comment's cross-reference to a budget the
                                deployment enforces at APISIX, not here

    A reference page that invents three knobs is worse than no page, so the AST
    is the source rather than the file text: comments are absent from it
    entirely, and docstrings are skipped explicitly. Ordinary string literals are
    kept — plenty of real vars are declared as constants
    (``HTTP_TRANSPORT_ENV_VAR = "WITAN_OMNIGRAPH_HTTP"``), and dropping those
    would trade three false positives for a dozen false negatives.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
        # PEP 257 "attribute docstrings" — a bare string after an assignment.
        # These packages use them heavily to document constants, and they are
        # where two of the three phantoms above came from.
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if not isinstance(stmts, list):
                continue
            for prev, cur in itertools.pairwise(stmts):
                if (
                    isinstance(prev, (ast.Assign, ast.AnnAssign))
                    and isinstance(cur, ast.Expr)
                    and isinstance(cur.value, ast.Constant)
                    and isinstance(cur.value.value, str)
                ):
                    docstrings.add(id(cur.value))

    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.update(_ENV_RE.findall(node.value))
    return found


def discover_env_vars() -> set[str]:
    """Every ``WITAN_*`` setting shipped (non-test) source actually reads."""
    found: set[str] = set()
    roots = [
        REPO_ROOT / "packages/witan-core/witan_core",
        REPO_ROOT / "mcp/servers/witan/witan",
        REPO_ROOT / "mcp/servers/witan-code/witan_code",
        REPO_ROOT / "docker",
    ]
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix == ".py":
                found |= _py_env_names(path.read_text())
            elif path.suffix == ".sh":
                # Shell has no AST to lean on; stripping `#` comments is enough,
                # since the remaining `${VAR}` references are the real reads.
                text = "\n".join(
                    line.split("#", 1)[0] for line in path.read_text().splitlines()
                )
                found.update(_ENV_RE.findall(text))
    return {name for name in found if not _ENV_SKIP.search(name)}


@app.command(name="env")
def gen_env() -> None:
    """Generate the environment variable reference.

    Names are discovered from source; descriptions come from
    ``docs/_data/environment.toml``. A discovered name with no entry there is a
    hard error — that is the whole point of the split.
    """
    data = tomllib.loads((DATA / "environment.toml").read_text())
    documented = {
        name: entry
        for section in data.get("section", [])
        for name, entry in section.get("vars", {}).items()
    }
    discovered = discover_env_vars()

    if undocumented := sorted(discovered - set(documented)):
        raise SystemExit(
            "Environment variable(s) used in source but not documented in "
            "docs/_data/environment.toml:\n  " + "\n  ".join(undocumented)
        )
    if stale := sorted(set(documented) - discovered):
        raise SystemExit(
            "Environment variable(s) documented but no longer used in source "
            "(remove them from docs/_data/environment.toml):\n  " + "\n  ".join(stale)
        )

    body = [
        BANNER.format(source="source scan + docs/_data/environment.toml"),
        "# Environment variables\n",
        data["intro"].strip() + "\n",
    ]
    for section in data["section"]:
        body.append(f"## {section['title']}\n")
        if blurb := section.get("blurb"):
            body.append(blurb.strip() + "\n")
        body.append("| Variable | Default | Description |")
        body.append("| --- | --- | --- |")
        for name in sorted(section["vars"]):
            entry = section["vars"][name]
            default = entry.get("default", "")
            default = f"`{default}`" if default else "—"
            body.append(f"| `{name}` | {default} | {entry['desc'].strip()} |")
        body.append("")
    emit(REFERENCE / "environment.md", "\n".join(body))


# ── Mirrored package documentation ──────────────────────────────────

# Prose that already lives inside a package, and where it lands on the site.
#
# WHY MIRROR RATHER THAN MOVE. These files are shipped documentation for their
# package: `mcp/servers/witan/docs/USER_GUIDE.md` is what a PyPI reader or
# someone browsing the repo finds next to the code, and moving it into `docs/`
# to serve the site would take it away from them. Mirroring keeps one
# authoritative copy — the one next to the code — and makes the site's copy a
# build artifact, which `--check` then keeps honest.
#
# The two `CLI_REFERENCE.md` files are deliberately absent: `gen_cli` renders the
# same surface from the live command tree, so mirroring them would publish two
# CLI references that disagree the moment a flag changes.
MIRRORED: list[tuple[str, str]] = [
    ("mcp/servers/witan/docs/USER_GUIDE.md", "guides/witan-user-guide.md"),
    ("mcp/servers/witan/docs/write-path-scanning.md", "guides/write-path-scanning.md"),
    ("mcp/servers/witan/docs/migration-runbook.md", "guides/migration-runbook.md"),
    ("mcp/servers/witan/docs/deployed-witan-onboarding.md", "guides/deployed-witan.md"),
    ("mcp/servers/witan-code/docs/USER_GUIDE.md", "guides/witan-code-user-guide.md"),
    ("mcp/servers/witan-code/docs/BRANCH_INDEXING.md", "guides/branch-indexing.md"),
    (
        "mcp/servers/witan-code/docs/SYMBOL_FORMAT.md",
        "explanation/code-graph/symbol-format.md",
    ),
    (
        "mcp/servers/witan-code/docs/SYMBOL_TABLE.md",
        "explanation/code-graph/symbol-table.md",
    ),
    (
        "mcp/servers/witan-code/docs/PACKAGE_MAP.md",
        "explanation/code-graph/package-map.md",
    ),
    (
        "mcp/servers/witan-code/docs/EDGE_PRECISION_TIERS.md",
        "explanation/code-graph/edge-precision-tiers.md",
    ),
    (
        "mcp/servers/witan-code/docs/STAGE2_STITCHING.md",
        "explanation/code-graph/stage2-stitching.md",
    ),
]

MIRROR_BANNER = """<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit {source} instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`{source}`](https://github.com/mitodl/agent-kit/blob/main/{source}).

"""


GITHUB_BLOB = "https://github.com/mitodl/agent-kit/blob/main/"

_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(\s+\"[^\"]*\")?\)")


def rewrite_links(text: str, source_rel: str, dest_rel: str, mirror_map: dict) -> str:
    """Repoint a mirrored file's relative links so they still resolve on the site.

    A link like ``[CLI Reference](CLI_REFERENCE.md)`` is correct where the file
    lives, next to the code, and broken once the file is served from
    ``docs/guides/``. Two cases, two answers:

    * the target is **also mirrored** → rewrite to its site path, so the reader
      stays on the site and the link survives
    * the target is **not on the site** (a source file, a skill, a README) →
      rewrite to an absolute GitHub URL, which is where the reader wanted to end
      up anyway

    Doing this at mirror time rather than editing the source keeps one
    authoritative copy: the file next to the code stays correct for someone
    reading it in the repository.
    """
    source_dir = PurePosixPath(source_rel).parent
    dest_dir = PurePosixPath(dest_rel).parent

    def replace(match: re.Match) -> str:
        label, target, title = match.group(1), match.group(2), match.group(3) or ""
        if target.startswith(("http://", "https://", "#", "mailto:", "<")):
            return match.group(0)
        path, sep, fragment = target.partition("#")
        if not path:
            return match.group(0)

        resolved = os.path.normpath(str(source_dir / path))
        if resolved in mirror_map:
            # Both ends are on the site: emit a site-relative path.
            new = os.path.relpath(mirror_map[resolved], str(dest_dir))
        elif (REPO_ROOT / resolved).exists():
            new = GITHUB_BLOB + resolved
        else:
            # Neither a mirrored page nor a real repo path — leave it alone
            # rather than inventing a URL that 404s on GitHub instead of here.
            return match.group(0)
        return f"[{label}]({new}{sep}{fragment}{title})"

    return _MD_LINK.sub(replace, text)


def _mirror_map() -> dict[str, str]:
    """Repo path → site path, for every file this script mirrors."""
    mapping = {source: dest for source, dest in MIRRORED}
    adr_dir = REPO_ROOT / "mcp/servers/witan/docs/adr"
    for path in sorted(adr_dir.glob("*.md")):
        rel = str(path.relative_to(REPO_ROOT))
        mapping[rel] = f"explanation/decisions/{path.name}"
    return mapping


@app.command(name="mirror")
def gen_mirror() -> None:
    """Copy package-resident prose into the site tree, repointing its links."""
    mapping = _mirror_map()
    for source_rel, dest_rel in MIRRORED:
        source = REPO_ROOT / source_rel
        if not source.exists():
            raise SystemExit(
                f"Mirrored source missing: {source_rel}\n"
                f"It was moved or deleted — update MIRRORED in {Path(__file__).name}."
            )
        body = rewrite_links(source.read_text(), source_rel, dest_rel, mapping)
        emit(DOCS / dest_rel, MIRROR_BANNER.format(source=source_rel) + body)

    _mirror_adrs(mapping)


def _mirror_adrs(mapping: dict[str, str]) -> None:
    """Mirror the ADRs, and refuse to publish a duplicated decision number.

    ★ TWO NUMBERS ARE CURRENTLY USED TWICE — 0004 is both the Keycloak actor
    mapping and the optional task phase tag, and 0006 is both code-graph branch
    ownership and the stateless MCP protocol era. On a site that lists decisions
    by number that reads as a typo or a missing page, so it is surfaced here
    rather than quietly rendered.
    """
    adr_dir = REPO_ROOT / "mcp/servers/witan/docs/adr"
    by_number: dict[str, list[str]] = {}
    for path in sorted(adr_dir.glob("*.md")):
        by_number.setdefault(path.name.split("-")[0], []).append(path.name)
        source_rel = str(path.relative_to(REPO_ROOT))
        dest_rel = f"explanation/decisions/{path.name}"
        body = rewrite_links(path.read_text(), source_rel, dest_rel, mapping)
        emit(DOCS / dest_rel, MIRROR_BANNER.format(source=source_rel) + body)

    if dupes := {n: f for n, f in by_number.items() if len(f) > 1}:
        print(
            "  note: duplicate ADR number(s) — "
            + "; ".join(f"{n}: {', '.join(f)}" for n, f in sorted(dupes.items()))
        )


# ── Entry point ─────────────────────────────────────────────────────


@app.default
def main(*, check: bool = False) -> None:
    """Regenerate every reference page.

    Parameters
    ----------
    check: Do not write. Exit non-zero if any generated page is out of date,
        printing a diff. This is what CI runs.
    """
    global _check_mode
    _check_mode = check

    for step in (gen_mcp_tools, gen_cli, gen_schema, gen_env, gen_mirror):
        step()

    if check:
        if _stale:
            print(
                f"\n{len(_stale)} generated page(s) are out of date:\n  "
                + "\n  ".join(_stale)
                + "\n\nRun `just docs-gen` and commit the result.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print("All generated documentation is up to date.")


if __name__ == "__main__":
    app()
