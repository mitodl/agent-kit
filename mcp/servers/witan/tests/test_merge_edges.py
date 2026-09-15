"""Merging and rebuilding stores whose edges are keyed on their endpoints.

From omnigraph 0.11 (storage format 9) every witan edge type is keyed on
``(@src, @dst)``. A loaded edge replaces the target's row for its pair, a
0.10 export's edge ULID is refused, and a duplicate pair fails a whole load —
see docs/internals/design/omnigraph-0-11-upgrade-spec.md, D2.
"""

import json
import re
import subprocess

from .conftest import SCHEMA, _Tools, requires_omnigraph

# 2026-01-01T00:00:00Z and 2026-01-02T00:00:00Z as omnigraph 0.10 exports them.
_JAN_1_MS = 1767225600000
_JAN_2_MS = 1767312000000


def _edge(confidence=None, created_at=None, role=None):
    return {
        "edge": "Tagged",
        "from": "mem-edge-aaaaaa",
        "to": "tp-topic-edges",
        "data": {"confidence": confidence, "created_at": created_at, "role": role},
    }


def _key(row):
    return (row["edge"], row["from"], row["to"])


def test_an_edge_the_target_lacks_is_added():
    from witan import server as srv

    source = _edge(confidence="inferred")

    decisions, winners = srv._reconcile_edges({_key(source): source}, {})

    assert [d["decision"] for d in decisions] == ["added"]
    assert winners == [source]


def test_a_stronger_source_edge_updates_the_target_copy():
    from witan import server as srv

    source = _edge(confidence="asserted", created_at=_JAN_1_MS, role="named")
    target = _edge(confidence="inferred", created_at=_JAN_2_MS)

    decisions, winners = srv._reconcile_edges(
        {_key(source): source}, {_key(target): target}
    )

    assert [d["decision"] for d in decisions] == ["updated"]
    assert winners == [source]


def test_a_weaker_source_edge_leaves_the_target_copy_alone():
    """The case passing edges through got wrong: on a keyed graph the load
    would replace the target row, erasing its role and provenance."""
    from witan import server as srv

    source = _edge()
    target = _edge(confidence="asserted", created_at=_JAN_1_MS, role="named")

    decisions, winners = srv._reconcile_edges(
        {_key(source): source}, {_key(target): target}
    )

    assert [d["decision"] for d in decisions] == ["kept-target"]
    assert winners == []


def test_an_identical_edge_keeps_the_target_so_a_rerun_loads_nothing():
    from witan import server as srv

    row = _edge(confidence="asserted", created_at=_JAN_1_MS)

    decisions, winners = srv._reconcile_edges({_key(row): row}, {_key(row): dict(row)})

    assert [d["decision"] for d in decisions] == ["kept-target"]
    assert winners == []


def test_an_edge_decision_renders_like_a_node_decision():
    """`type` and `slug` are what every decision renderer reads, including a
    CLI released before edges were reconciled."""
    from witan import server as srv

    source = _edge()

    (decision,), _ = srv._reconcile_edges({_key(source): source}, {})

    assert decision["type"] == "Tagged"
    assert decision["slug"] == "mem-edge-aaaaaa -> tp-topic-edges"
    assert (decision["edge"], decision["from"], decision["to"]) == _key(source)


@requires_omnigraph
def test_store_merge_reconciles_an_edge_against_the_deployed_copy(server):
    from witan import server as srv

    tools = _Tools(srv)
    srv.client.change(
        "mutations.gq",
        "insert_topic",
        {
            "slug": "tp-topic-edges",
            "name": "edges",
            "kind": "topic",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    slug = tools.memory_store(
        kind="lesson", title="edge target", content="an edge lands here"
    )["slug"]
    srv.client.change(
        "mutations.gq",
        "link_tagged",
        {
            "from": slug,
            "to": "tp-topic-edges",
            "confidence": "inferred",
            "role": None,
            "author": None,
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    stronger = {**_edge(confidence="asserted", created_at=_JAN_2_MS), "from": slug}
    unseen = {**_edge(), "from": slug, "to": "tp-topic-other"}

    result = tools.store_merge(rows=[stronger, unseen], dry_run=True)

    by_pair = {(d["from"], d["to"]): d["decision"] for d in result["decisions"]}
    assert by_pair == {
        (slug, "tp-topic-edges"): "updated",
        (slug, "tp-topic-other"): "added",
    }
    assert (result["updated"], result["added"], result["passthrough"]) == (1, 1, 0)


def _keyed(schema_text: str) -> str:
    """The bundled schema with every edge type keyed on its endpoints.

    Left as-is once the schema declares keys itself, so this keeps testing the
    shipped schema rather than a copy of it.
    """
    if "@key(@src, @dst)" in schema_text:
        return schema_text
    braced = re.sub(
        r"^(edge \w+: \w+ -> \w+) \{$",
        r"\1 {\n    @key(@src, @dst)",
        schema_text,
        flags=re.MULTILINE,
    )
    return re.sub(
        r"^(edge \w+: \w+ -> \w+)$",
        r"\1 { @key(@src, @dst) }",
        braced,
        flags=re.MULTILINE,
    )


# An `omnigraph export` exactly as 0.10.0 wrote it for a store holding the
# duplicate Tagged pair unkeyed edges accumulate: identity in `data.id` on
# every row, DateTimes as epoch milliseconds, unset optionals as explicit nulls.
_EXPORT_010 = [
    {
        "edge": "Blocks",
        "from": "tk-two",
        "to": "tk-one",
        "data": {"id": "01M2KBRXAH8DW63NBW77DCF2TH"},
    },
    {
        "edge": "RelatedTo",
        "from": "pat-a",
        "to": "les-b",
        "data": {
            "id": "01M2KBRXAHRB1D0AH4NNNYE8E8",
            "author": None,
            "confidence": None,
            "created_at": None,
            "role": None,
        },
    },
    {
        "edge": "Tagged",
        "from": "pat-a",
        "to": "tp-topic-uv",
        "data": {
            "id": "01M2KBRXAH26XMG18YDFJZ7WF4",
            "author": "t",
            "confidence": "inferred",
            "created_at": _JAN_2_MS,
            "role": None,
        },
    },
    {
        "edge": "Tagged",
        "from": "pat-a",
        "to": "tp-topic-uv",
        "data": {
            "id": "01M2KBRXAHMXESN91PPC184V38",
            "author": "t",
            "confidence": "asserted",
            "created_at": _JAN_1_MS,
            "role": "named",
        },
    },
    {
        "type": "Memory",
        "data": {
            "id": "les-b",
            "author": "t",
            "category": None,
            "confidence": None,
            "content": "beta",
            "created_at": _JAN_2_MS,
            "kind": "lesson",
            "language": None,
            "repo": "r",
            "severity": "warning",
            "slug": "les-b",
            "symbol_refs": None,
            "tags": None,
            "title": "B",
            "updated_at": _JAN_2_MS,
        },
    },
    {
        "type": "Memory",
        "data": {
            "id": "pat-a",
            "author": "t",
            "category": None,
            "confidence": 0.5,
            "content": "alpha",
            "created_at": _JAN_1_MS,
            "kind": "pattern",
            "language": "python",
            "repo": None,
            "severity": None,
            "slug": "pat-a",
            "symbol_refs": None,
            "tags": ["uv", "x"],
            "title": "A",
            "updated_at": _JAN_1_MS,
        },
    },
    {
        "type": "Task",
        "data": {
            "id": "tk-one",
            "assignee": None,
            "author": "t",
            "blocked_by": ["tk-two"],
            "claimed_at": None,
            "closed_at": None,
            "created_at": _JAN_1_MS,
            "description": "d",
            "external_uri": None,
            "parent_slug": None,
            "priority": "p1",
            "project_slug": None,
            "repo": None,
            "resolution": None,
            "slug": "tk-one",
            "status": "open",
            "symbol_refs": None,
            "tags": None,
            "title": "one",
            "type": "task",
            "updated_at": _JAN_1_MS,
        },
    },
    {
        "type": "Task",
        "data": {
            "id": "tk-two",
            "assignee": "a",
            "author": "t",
            "blocked_by": None,
            "claimed_at": None,
            "closed_at": _JAN_2_MS,
            "created_at": _JAN_1_MS,
            "description": "d",
            "external_uri": None,
            "parent_slug": None,
            "priority": "p0",
            "project_slug": None,
            "repo": None,
            "resolution": "done",
            "slug": "tk-two",
            "status": "closed",
            "symbol_refs": None,
            "tags": None,
            "title": "two",
            "type": "bug",
            "updated_at": _JAN_1_MS,
        },
    },
    {
        "type": "Topic",
        "data": {
            "id": "tp-topic-uv",
            "created_at": _JAN_1_MS,
            "kind": "topic",
            "name": "uv",
            "slug": "tp-topic-uv",
        },
    },
]


@requires_omnigraph
def test_a_010_export_with_duplicate_edges_rebuilds_into_a_keyed_graph(tmp_path):
    """The load half of `migrate_storage_format`, against the real binary.

    Loaded raw, this export fails three ways on a keyed format-9 graph: the
    node ids sit in `data.id`, the edge ULIDs are not the edges' canonical
    ids, and the Tagged pair appears twice.
    """
    from witan import server as srv

    exported = tmp_path / "export.jsonl"
    exported.write_text("".join(json.dumps(r) + "\n" for r in _EXPORT_010))
    data_file = tmp_path / "graph.jsonl"
    schema = tmp_path / "keyed.pg"
    schema.write_text(_keyed(SCHEMA.read_text()))
    store = tmp_path / "rebuilt.omni"

    collapsed = srv._rewrite_export_for_load(exported, data_file)

    subprocess.run(
        ["omnigraph", "init", "--schema", str(schema), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "omnigraph",
            "load",
            "--store",
            str(store),
            "--data",
            str(data_file),
            "--mode",
            "overwrite",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    out = subprocess.run(
        ["omnigraph", "export", "--store", str(store)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]

    assert collapsed == 1
    counts = {}
    for row in rows:
        table = row.get("type") or row["edge"]
        counts[table] = counts.get(table, 0) + 1
    assert counts == {
        "Memory": 2,
        "Task": 2,
        "Topic": 1,
        "Tagged": 1,
        "RelatedTo": 1,
        "Blocks": 1,
    }
    (tagged,) = [r for r in rows if r.get("edge") == "Tagged"]
    # The older asserted link survives the newer inferred one, whole.
    assert tagged["data"]["confidence"] == "asserted"
    assert tagged["data"]["role"] == "named"
    assert json.loads(tagged["id"]) == ["pat-a", "tp-topic-uv"]
