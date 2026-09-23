#!/usr/bin/env -S uv run --quiet --all-packages --python 3.12 python
"""Record what witan's bound read tools actually return, for the UI to test against.

WHY THIS EXISTS. The UI's result types are hand-written, and they have to be:
every bound tool is registered with no ``output_schema=``, so fastmcp derives
one from the return annotation and gets ``{"type": "object",
"additionalProperties": true}`` or a ``result`` wrapper around an untyped
array. There is nothing to generate types from. So the types are written by
hand and kept honest from the other side: these fixtures are real tool results,
``all-fixtures.test.ts`` walks every one of them through the unwrapper,
``fixtures.test.ts`` asserts the specific shapes the views lean on, and a
server change that renames a field fails the frontend tests in the same PR
that made it.

``tools-list.json`` is the other half and is not decoration. The unwrapper keys
on each tool's ``x-fastmcp-wrap-result`` flag rather than guessing from the
shape of a result, and an MCP Apps widget (spec §7) is handed a result with no
``tools/list`` to consult, so the flags have to be recorded at build time.

THE INTERPRETER IS PINNED IN THE SHEBANG so that everyone and CI record
against the same environment. Note this is NOT ``gen_docs.py``'s reason: that
one records tool INPUT schemas, where fastmcp's ``Literal`` enum ordering
differs between Pythons. Every schema recorded here is an untyped
``object``/``array`` (see above), so there is no enum to order. The pin is
cheap insurance rather than a fix for a known difference.

Usage:
    ./bin/gen_ui_fixtures.py          # regenerate
    ./bin/gen_ui_fixtures.py --check  # fail if stale (CI)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "mcp" / "servers" / "witan" / "ui" / "fixtures"
SCHEMA = REPO / "mcp" / "servers" / "witan" / "schema" / "schema.pg"

# ADR 0011 §3's bound set, which is the whole read surface the UI may call.
# Adding anything here means amending the ADR first, as its 2026-09-22
# amendment did for `memory_contradictions`.
BOUND_TOOLS = (
    "task_ready",
    "task_get",
    "task_list",
    "task_search",
    "workflow_project_get",
    "workflow_project_status",
    "workflow_project_list",
    "workflow_session_list",
    "recall",
    "memory_get",
    "memory_list",
    "memory_search",
    "memory_neighbors",
    "memory_contradictions",
    "topic_get",
    "code_repo_dependencies",
    "code_interface_providers",
    "code_interface_consumers",
)

# Bound tools a server may legitimately lack: witan-code is a separate package,
# mounted only when installed (ADR 0011, 2026-09-23 amendment). Recorded as
# optional so the page tolerates their absence instead of failing its
# once-per-load check against `tools/list`.
OPTIONAL_TOOLS = frozenset(
    {"code_repo_dependencies", "code_interface_providers", "code_interface_consumers"}
)

REPO_URI = "https://github.com/mitodl/agent-kit"

# The bridge seed's repos. Fictional, so the fixtures carry nobody's code.
_INFRA = "https://github.com/example/infra"
_WEB = "https://github.com/example/web"
_API = "https://github.com/example/api"


class _NoElicitCtx:
    """A Context whose ``elicit`` always errors.

    The write tools used for seeding are ``async`` only so they can offer an
    elicitation. Refusing it is what a non-interactive client does, and makes
    them take their default path.
    """

    async def elicit(self, *args, **kwargs):
        msg = "elicitation unsupported while recording fixtures"
        raise RuntimeError(msg)


async def _seed(srv) -> dict[str, str]:
    """A small graph with every shape the views read.

    Deliberately not a snapshot of anyone's real store: fixtures are committed,
    so they have to be reproducible and carry nobody's content.
    """

    async def call(name, **kwargs):
        """Call a tool by name, awaiting the async ones.

        Several write tools are ``async`` (they can elicit). The test suite's
        ``_Tools`` proxy drives those with ``asyncio.run``, which cannot be
        used from inside the loop this already runs on.
        """
        fn = getattr(srv, name)
        fn = getattr(fn, "fn", fn)
        if inspect.iscoroutinefunction(fn):
            kwargs.setdefault("ctx", _NoElicitCtx())
            return await fn(**kwargs)
        return fn(**kwargs)

    project = await call(
        "workflow_project_create",
        title="Witan UI",
        description="A human-readable surface for the graph.",
    )
    epic = await call(
        "task_create",
        title="Witan UI epic",
        description="Parent of the UI work.",
        type="epic",
        project_slug=project["slug"],
    )
    blocker = await call(
        "task_create",
        title="Close the read gaps",
        description="task_list limit, list timestamps, task_get edges.",
        project_slug=project["slug"],
        parent=epic["slug"],
        priority="p1",
    )
    blocked = await call(
        "task_create",
        title="Board view driven by task_ready",
        description="Claim lease age and stale-claim marks.",
        project_slug=project["slug"],
        parent=epic["slug"],
        blocked_by=[blocker["slug"]],
        priority="p2",
    )
    held = await call(
        "task_create",
        title="Scaffold the frontend package",
        description="Vite, lit-html, vitest.",
        project_slug=project["slug"],
        priority="p1",
    )
    await call(
        "task_claim",
        slug=held["slug"],
        assignee="fixture-agent",
        branch="witan-ui-scaffold",
    )
    await call(
        "task_comment",
        slug=blocker["slug"],
        text="Counting one past the cap is not the same as counting.",
    )

    session = await call(
        "workflow_session_start",
        project_slug=project["slug"],
        session_id="fixture-session",
        phase="implementation",
    )
    await call(
        "workflow_session_end",
        session_slug=session["session_slug"],
        summary="Recorded the fixtures.",
    )

    memory = await call(
        "memory_store",
        kind="pattern",
        title="Unwrap MCP results on the wrap flag, not on the shape",
        content=(
            "fastmcp wraps list and dict|None returns as "
            "structuredContent {'result': ...} and marks the schema with "
            "x-fastmcp-wrap-result. Guessing from the value's shape gets a "
            "bare dict return wrong."
        ),
        tags=["mcp", "witan-ui"],
    )
    # A `project_fact` deliberately: its `pf-` slug is the prefix the first
    # version of `_SLUG_PREFIXES` missed, and seeding one is what turns that
    # from a latent always-failing gate into a caught mistake.
    await call(
        "memory_store",
        kind="project_fact",
        title="The UI reads through the tools, not through read.gq",
        content=(
            "The composition every view needs (the lease rule, the project "
            "rollup, recall's ranking) lives in server.py, not in the queries."
        ),
        tags=["witan-ui"],
    )
    other = await call(
        "memory_store",
        kind="lesson",
        title="A silent cap is worse than a small one",
        content=(
            "An unscoped task_list returned 50 rows and said nothing about the rest."
        ),
        tags=["witan-ui"],
    )
    # `contradicts` rather than `related_to`: it is what the memory view's
    # contradictions inbox reads, and it is the one edge kind `recall` reports
    # on its own, so recording it here gives that fixture a non-empty
    # `contradictions` list to type against.
    await call(
        "memory_link",
        from_slug=memory["slug"],
        to_slug=other["slug"],
        kind="contradicts",
    )
    # A memory the pattern replaced, so `memory_neighbors` records both sides
    # of `supersedes` and the default reads have something to prune.
    replaced = await call(
        "memory_store",
        kind="pattern",
        title="Unwrap MCP results by looking for a result key",
        content="Take structuredContent['result'] whenever it is there.",
    )
    await call(
        "memory_link",
        from_slug=memory["slug"],
        to_slug=replaced["slug"],
        kind="supersedes",
        role="the shape test misreads a bare dict",
    )

    return {
        "project": project["slug"],
        "epic": epic["slug"],
        "blocker": blocker["slug"],
        "blocked": blocked["slug"],
        "held": held["slug"],
        "memory": memory["slug"],
        "replaced": replaced["slug"],
    }


# Every slug carries a random suffix and every node carries timestamps, so a
# raw recording differs on every run and `--check` would report stale fixtures
# nobody touched — a gate that always fails is not a gate. Normalizing keeps
# what the types are actually checked against (the field names, the nesting,
# null vs [] vs a value) and throws away only the parts that cannot be stable.
_EPOCH = "2026-01-01T00:00:00+00:00"
_ISO = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?$"
)
# The generated-slug prefixes witan actually mints, from `_KIND_PREFIX` in
# server.py plus its "mem" fallback. Keep this in step with that table: a
# prefix missing here is a slug that survives normalization, which makes
# `--check` fail on every run against files nobody touched. `pf`
# (project_fact) is the one that bites first, since it is the most ordinary
# memory kind there is.
_SLUG_PREFIXES = "pat|pf|les|ctx|wp|ws|wt|tk|tc|mem"
_SLUGGED = re.compile(rf"^({_SLUG_PREFIXES})-.+-[0-9a-f]{{6}}$")


def _seed_bridge() -> None:
    """A small cross-repo bridge: every edge shape the explorer draws.

    Written straight through ``bridge.write_bindings`` rather than by indexing
    real checkouts, so the seed is a handful of rows instead of three repos on
    disk. ``infra`` provides ``DATABASE_URL`` and the stoplisted ``PORT`` that
    ``web`` reads, and deploys ``web`` (a service edge); ``web`` calls an
    endpoint ``api`` serves and imports the client package ``api`` publishes.
    """
    from witan_code import bridge
    from witan_code import config as code_cfg
    from witan_code.bridge_extractors import ParsedBinding

    cfg = code_cfg.load()
    seeds = {
        _INFRA: [
            ParsedBinding(
                kind="env_var",
                key="DATABASE_URL",
                key_norm="DATABASE_URL",
                role="provider",
                file="src/web/__main__.py",
                line=12,
                language="python",
                framework="pulumi",
            ),
            ParsedBinding(
                kind="env_var",
                key="PORT",
                key_norm="PORT",
                role="provider",
                file="src/web/__main__.py",
                line=13,
                language="python",
                framework="pulumi",
                generic=True,
            ),
            ParsedBinding(
                kind="service",
                key=f"repo:{_WEB}",
                key_norm=f"repo:{_WEB}",
                role="provider",
                file="src/web/__main__.py",
                sub_kind="repo",
                line=20,
                language="python",
                framework="pulumi",
            ),
        ],
        _WEB: [
            ParsedBinding(
                kind="env_var",
                key="DATABASE_URL",
                key_norm="DATABASE_URL",
                role="consumer",
                file="web/settings.py",
                line=40,
                language="python",
                framework="django",
                symbol_id=f"{_WEB}#web/settings.py::DATABASES",
            ),
            ParsedBinding(
                kind="env_var",
                key="PORT",
                key_norm="PORT",
                role="consumer",
                file="web/settings.py",
                line=41,
                language="python",
                framework="django",
                generic=True,
            ),
            ParsedBinding(
                kind="endpoint",
                key="GET /api/v1/courses/42",
                key_norm="/api/v1/courses/{}",
                role="consumer",
                file="web/courses.py",
                line=8,
                language="python",
                symbol_id=f"{_WEB}#web/courses.py::fetch_course",
            ),
            ParsedBinding(
                kind="package",
                key="example-api-client",
                key_norm="example-api-client",
                role="consumer",
                file="pyproject.toml",
                line=9,
                language="toml",
            ),
        ],
        _API: [
            ParsedBinding(
                kind="endpoint",
                key="GET /api/v1/courses/{id}",
                key_norm="/api/v1/courses/{}",
                role="provider",
                file="api/views.py",
                line=30,
                language="python",
                framework="drf",
                symbol_id=f"{_API}#api/views.py::CourseView",
            ),
            ParsedBinding(
                kind="package",
                key="example-api-client",
                key_norm="example-api-client",
                role="provider",
                file="client/pyproject.toml",
                line=2,
                language="toml",
            ),
        ],
    }
    for repo, bindings in seeds.items():
        bridge.write_bindings(
            bindings,
            repo,
            cfg,
            full_repo=False,
            touched_files=tuple(sorted({b.file for b in bindings})),
        )


def _jsonable(value):
    """Round-trip through JSON before normalizing.

    `json.dumps(..., default=str)` would otherwise stringify a value (a
    `datetime`, say) AFTER `_normalize` had already walked past it, recording
    it raw and making `--check` fail on every run. That is the exact failure
    the normalizer exists to prevent, so the coercion has to happen first.
    """
    return json.loads(json.dumps(value, default=str))


def _normalize(value, seen: dict[str, str]):
    """Replace timestamps and generated slugs with stable stand-ins.

    ``seen`` is shared across every fixture in a run, so the same slug gets the
    same placeholder everywhere and the cross-references between fixtures
    (a task's ``project_slug``, a comment's ``task_slug``) stay meaningful.
    """
    if isinstance(value, dict):
        return {k: _normalize(v, seen) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v, seen) for v in value]
    if isinstance(value, str):
        if _ISO.match(value):
            return _EPOCH
        if _SLUGGED.match(value):
            prefix = value.split("-", 1)[0]
            return seen.setdefault(value, f"{prefix}-fixture-{len(seen):03d}")
    return value


def _calls(slugs: dict[str, str]) -> dict[str, dict]:
    """The one call per bound tool whose result gets recorded.

    Chosen to be the shape a view actually issues: `repo=""` everywhere,
    because §6.1 has every call pass repo explicitly so a result never depends
    on the server's working directory.
    """
    return {
        "task_ready": {"repo": "", "limit": 20},
        "task_get": {"slug": slugs["blocker"]},
        "task_list": {"repo": "", "limit": 50},
        "task_search": {"query": "read gaps", "repo": ""},
        "workflow_project_get": {"slug": slugs["project"]},
        "workflow_project_status": {"slug": slugs["project"]},
        "workflow_project_list": {"repo": ""},
        "workflow_session_list": {"project_slug": slugs["project"]},
        "recall": {"query": "unwrap mcp results", "repo": ""},
        # With topics: the memory view shows them, and their edges are the
        # only `inferred` ones the seed produces (promoted from `tags`).
        "memory_get": {"slug": slugs["memory"], "include_topics": True},
        "memory_list": {"kind": "pattern", "repo": ""},
        "memory_search": {"query": "wrap flag", "repo": ""},
        "memory_neighbors": {"slug": slugs["memory"]},
        "memory_contradictions": {"repo": ""},
        # `name:kind`, not a bare tag: a plain name resolves nothing and the
        # fixture would record the null case twice over.
        "topic_get": {"topic": "witan-ui:topic"},
        # `repo` explicit, as for every other tool: "" is every repo.
        "code_repo_dependencies": {"repo": ""},
        # `kind` and `key` explicit: the ADR 0011 amendment's rule, and what
        # an edge's contract hands the drill-down.
        "code_interface_providers": {"kind": "env_var", "key": "DATABASE_URL"},
        "code_interface_consumers": {"kind": "env_var", "key": "DATABASE_URL"},
    }


async def _collect(store: Path) -> dict[str, str]:
    """Drive the real tools over an in-memory MCP client and record the results.

    Through `fastmcp.Client` rather than by calling the functions directly,
    because what the UI parses is the MCP envelope — `structuredContent` and
    the wrap flag — and calling the Python function returns the value before
    any of that is applied.
    """
    from fastmcp import Client

    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    srv.client = graph_mod.OmnigraphClient(str(store), cfg_mod.load().queries_dir)

    # Mounted as `witan serve` and `witan ui` mount it, so the code tools are
    # recorded through the same server the page talks to.
    from witan_code.server import mcp as code_mcp

    srv.mcp.mount(code_mcp)
    _seed_bridge()

    slugs = await _seed(srv)
    written: dict[str, str] = {}

    async with Client(srv.mcp) as client:
        listed = await client.list_tools()
        by_name = {t.name: t for t in listed}
        missing = [name for name in BOUND_TOOLS if name not in by_name]
        if missing:
            msg = f"bound tools are not registered on the server: {missing}"
            raise SystemExit(msg)

        seen: dict[str, str] = {}

        written["tools-list.json"] = json.dumps(
            {
                name: {
                    # The ONE field the unwrapper reads. Recorded per tool
                    # because a widget is handed a result with no tools/list.
                    "wrapped": bool(
                        (by_name[name].output_schema or {}).get(
                            "x-fastmcp-wrap-result", False
                        )
                    ),
                    "output_schema": by_name[name].output_schema,
                    # Whether a server may lack it. See OPTIONAL_TOOLS.
                    "optional": name in OPTIONAL_TOOLS,
                }
                for name in BOUND_TOOLS
            },
            indent=2,
            sort_keys=True,
        )

        for name, args in _calls(slugs).items():
            result = await client.call_tool(name, args)
            written[f"{name}.json"] = json.dumps(
                _normalize(_jsonable(result.structured_content), seen),
                indent=2,
                sort_keys=True,
                default=str,
            )

        # The not-found case gets its own fixture. Discovery found the CLI
        # getting exactly this wrong (prose where JSON belonged), and a client
        # that treats "no such task" as an error is a page that shows a crash
        # for a stale link.
        missing_result = await client.call_tool(
            "task_get", {"slug": "tk-does-not-exist"}
        )
        written["task_get.missing.json"] = json.dumps(
            _normalize(_jsonable(missing_result.structured_content), seen),
            indent=2,
            sort_keys=True,
            default=str,
        )

        # The superseded side, whose `superseded_by` is the only non-empty
        # inbound group, and the reason a reader landing here needs the view.
        replaced_result = await client.call_tool(
            "memory_neighbors", {"slug": slugs["replaced"]}
        )
        written["memory_neighbors.superseded.json"] = json.dumps(
            _normalize(_jsonable(replaced_result.structured_content), seen),
            indent=2,
            sort_keys=True,
            default=str,
        )

        empty_result = await client.call_tool(
            "task_list", {"repo": "https://github.com/example/nothing-here"}
        )
        written["task_list.empty.json"] = json.dumps(
            _normalize(_jsonable(empty_result.structured_content), seen),
            indent=2,
            sort_keys=True,
            default=str,
        )

    return written


def _generate() -> dict[str, str]:
    """Build a throwaway graph, record against it, and never touch a real store."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        store = home / "graph.omni"
        subprocess.run(
            ["omnigraph", "init", "--schema", str(SCHEMA), str(store)],
            check=True,
            capture_output=True,
            text=True,
        )
        # Set before witan.server is imported: it builds a client at import.
        os.environ.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "WITAN_MEMORY_URI": str(store),
                "WITAN_REPO": REPO_URI,
                "WITAN_AUTHOR": "fixtures",
                "WITAN_OPTIMIZE_INTERVAL": "0",
            }
        )
        # witan-code's store in the throwaway home too, and nothing that would
        # point it at a remote code server instead.
        os.environ["WITAN_CODE_DIR"] = str(home / "code")
        for stale in (
            "CLAUDE_SESSION_ID",
            "WITAN_REMOTE_URL",
            "WITAN_TARGET",
            "WITAN_CODE_SERVER",
            "WITAN_CODE_TOKEN",
            "WITAN_CODE_TRANSPORT",
            "WITAN_CODE_INDEX_ROLE",
        ):
            os.environ.pop(stale, None)
        written = asyncio.run(_collect(store))
    written["repo-keys.json"] = _repo_keys()
    written["graph.json"] = _graph(written)
    return written


# Remotes in every shape `witan_core.repo_key.normalise` handles, including the
# ones whose handling is not obvious (userinfo, a slash after `.git`, a host
# whose path keeps its case). The OUTPUTS are recorded from the live function,
# so this list only has to cover the cases; it cannot drift from the rule.
_REPO_KEY_INPUTS = (
    "git@github.com:mitodl/ol-django.git",
    "https://github.com/mitodl/ol-django",
    "https://github.com/mitodl/ol-django.git",
    "git@gitlab.com:grp/sub/repo.git",
    "https://x-token@github.com/mitodl/repo.git",
    "ssh://git@github.com/mitodl/repo.git",
    "https://github.com/mitodl/repo/",
    "https://github.com/mitodl/repo.git/",
    "git@github.com:mitodl/repo.git/",
    "some-bare-string",
    "https://github.com/MITODL/OL-Django",
    "https://GitHub.com/mitodl/ol-django",
    "git@github.com:MITODL/OL-Django.git",
    "git@gitlab.com:Grp/Sub/Repo.git",
    "https://Git.example.com/Org/Repo",
    "http://github.com/mitodl/repo",
    "  https://github.com/mitodl/repo  ",
)


def _repo_keys() -> str:
    """``normalise`` over ``_REPO_KEY_INPUTS``, for the UI's copy to match.

    The UI canonicalizes a route's repo in the browser (``canonicalRepo`` in
    ``ui/src/format.ts``) so its columns compare the same key the tools scope
    by. That is a second copy of a rule its own module calls a golden
    contract, so the copy is held to the real function here rather than to a
    table someone has to remember to update.
    """
    from witan_core import repo_key

    return json.dumps(
        {url: repo_key.normalise(url) for url in _REPO_KEY_INPUTS},
        indent=2,
        sort_keys=True,
    )


def _graph(written: dict[str, str]) -> str:
    """``witan graph``'s nodes and edges over the recorded list fixtures.

    The Graph tab ports ``scope_tasks`` and ``build_graph`` to TypeScript
    (spec §6.8), and this is what holds the port to the CLI: the same two
    recorded reads through the Python transform, for ``graph.test.ts`` to
    compare against. The tooltip and ``detail`` are left out because they are
    presentation the two renderers do differently (HTML vs. plain text, the
    whole row vs. the shared panel).
    """
    from witan import visualize

    projects = json.loads(written["workflow_project_list.json"])["result"]
    tasks = json.loads(written["task_list.json"])["result"]
    graph = visualize.build_graph(projects, visualize.scope_tasks(projects, tasks))
    return json.dumps(
        {
            "nodes": [
                {
                    "id": n.id,
                    "label": n.label,
                    "group": n.group,
                    "color": n.color,
                    "status": n.status,
                }
                for n in graph.nodes
            ],
            "edges": [
                {"src": e.src, "dst": e.dst, "kind": e.kind, "label": e.label}
                for e in graph.edges
            ],
        },
        indent=2,
        sort_keys=True,
    )


def main() -> int:
    check = "--check" in sys.argv
    written = _generate()

    if check:
        stale = [
            name
            for name, body in written.items()
            if not (FIXTURES / name).is_file()
            or (FIXTURES / name).read_text() != body + "\n"
        ]
        # A fixture on disk that nothing generated any more: a tool dropped
        # from BOUND_TOOLS leaves its recording behind, and comparing only
        # what was generated would keep the gate green over a stale file.
        orphaned = {
            path.name for path in FIXTURES.glob("*.json") if path.name not in written
        }
        if stale or orphaned:
            if stale:
                print("UI fixtures are stale:", file=sys.stderr)
                for name in sorted(stale):
                    print(f"  {name}", file=sys.stderr)
            if orphaned:
                print("UI fixtures no tool produces any more:", file=sys.stderr)
                for name in sorted(orphaned):
                    print(f"  {name}", file=sys.stderr)
            print("\nRun `just ui-fixtures` and commit the result.", file=sys.stderr)
            return 1
        print(f"All {len(written)} UI fixtures are up to date.")
        return 0

    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, body in sorted(written.items()):
        (FIXTURES / name).write_text(body + "\n")
        print(f"  wrote {(FIXTURES / name).relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
