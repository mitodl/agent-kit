"""Unit tests for mutation batching — splicing many statements into one commit.

The measured reason this exists: on omnigraph 0.8.1, 20 single-row mutates cost
1.85s and 20 Lance versions; one 20-statement mutate costs 0.095s and one.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from witan_core import omnigraph as og
from witan_core.omnigraph import OmnigraphClient

_SOURCE = """\
// a comment mentioning query not_a_query( and a stray brace {
query insert_thing(
    $slug: String,
    $tags: [String]?,
    $note: String?
) {
    insert Thing {
        slug: $slug,
        tags: $tags,
        note: $note
    }
}

query link_to($from: String, $to: String) {
    insert LinksTo { from: $from, to: $to }
}
"""


def _source(_name):
    return _SOURCE


def _client(monkeypatch, tmp_path, **kwargs):
    (tmp_path / "m.gq").write_text(_SOURCE)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    return OmnigraphClient(str(tmp_path / "g.omni"), tmp_path, **kwargs)


def _record(monkeypatch, calls):
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)


# ── parsing the .gq source ─────────────────────────────────────────


def test_parse_query_extracts_params_and_body_past_nested_braces():
    params, body = og.parse_query(_SOURCE, "insert_thing")
    assert "$slug: String" in params
    assert body.startswith("insert Thing {")
    # the NEXT query must not bleed in through the nested closing brace
    assert "LinksTo" not in body


def test_parse_query_ignores_a_query_keyword_inside_a_comment():
    # the comment names `query not_a_query(`; a scan that reads comments would
    # match it and then splice from the wrong offset
    params, _ = og.parse_query(_SOURCE, "link_to")
    assert params == "$from: String, $to: String"


def test_parse_query_raises_on_an_unknown_name():
    with pytest.raises(KeyError):
        og.parse_query(_SOURCE, "nope")


# ── composing the batch ────────────────────────────────────────────


def test_compose_batch_prefixes_each_step_so_shared_names_do_not_collide():
    source, params = og.compose_batch(
        [
            ("m.gq", "insert_thing", {"slug": "a", "tags": None, "note": None}),
            ("m.gq", "insert_thing", {"slug": "b", "tags": ["t"], "note": "n"}),
            ("m.gq", "link_to", {"from": "a", "to": "b"}),
        ],
        _source,
    )
    assert params == {
        "s0_slug": "a",
        "s0_tags": None,
        "s0_note": None,
        "s1_slug": "b",
        "s1_tags": ["t"],
        "s1_note": "n",
        "s2_from": "a",
        "s2_to": "b",
    }
    decls, body = source.split(")", 1)[0], source.split("{", 1)[1]
    # nothing the body references may be left undeclared, and nothing declared
    # may be left unsupplied — either way omnigraph fails on generated source
    assert set(og._PARAM_REF_RE.findall(body)) <= set(og._PARAM_REF_RE.findall(decls))
    assert set(og._PARAM_REF_RE.findall(decls)) == set(params)
    assert source.count("insert ") == 3
    assert source.count("query ") == 1


def test_compose_batch_names_a_missing_parameter():
    # dropping the declaration instead would leave the body referencing an
    # unbound variable — the failure this check exists to pre-empt
    with pytest.raises(KeyError, match="note"):
        og.compose_batch(
            [("m.gq", "insert_thing", {"slug": "a", "tags": None})], _source
        )


def test_compose_batch_prefixes_from_zero():
    # `$slug` in the FIRST step is `$s0_slug`, not `$s1_slug` — the prefix is
    # the 0-based position, and getting this backwards makes composed params
    # unreadable when debugging
    _, params = og.compose_batch(
        [
            ("m.gq", "link_to", {"from": "a", "to": "b"}),
            ("m.gq", "link_to", {"from": "c", "to": "d"}),
        ],
        _source,
    )
    assert params == {
        "s0_from": "a",
        "s0_to": "b",
        "s1_from": "c",
        "s1_to": "d",
    }


# ── the client method ──────────────────────────────────────────────


def test_change_many_issues_one_mutate_for_many_steps(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    _record(monkeypatch, calls)
    client.change_many(
        [
            ("m.gq", "insert_thing", {"slug": "a", "tags": None, "note": None}),
            ("m.gq", "link_to", {"from": "a", "to": "a"}),
        ]
    )
    assert len(calls) == 1
    inline = calls[0][calls[0].index("-e") + 1]
    assert inline.count("insert ") == 2


def test_change_many_reads_each_query_file_once(monkeypatch, tmp_path):
    # every step of a real batch comes from the same .gq; re-reading it per
    # step would put avoidable I/O on the path that exists to be faster
    client = _client(monkeypatch, tmp_path)
    _record(monkeypatch, [])
    reads: list[str] = []
    real_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        reads.append(self.name)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    client.change_many(
        [
            ("m.gq", "insert_thing", {"slug": "a", "tags": None, "note": None}),
            ("m.gq", "link_to", {"from": "a", "to": "a"}),
            ("m.gq", "link_to", {"from": "a", "to": "a"}),
        ]
    )
    assert reads.count("m.gq") == 1


def test_change_many_preserves_step_order(monkeypatch, tmp_path):
    # an edge resolves its endpoints against the statements AHEAD of it, so a
    # reordering batcher would break every node-then-edge caller
    client = _client(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    _record(monkeypatch, calls)
    client.change_many(
        [
            ("m.gq", "insert_thing", {"slug": "a", "tags": None, "note": None}),
            ("m.gq", "link_to", {"from": "a", "to": "a"}),
        ]
    )
    inline = calls[0][calls[0].index("-e") + 1]
    assert inline.index("insert Thing") < inline.index("insert LinksTo")


def test_change_many_passes_a_single_step_through_to_change(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    _record(monkeypatch, calls)
    client.change_many([("m.gq", "link_to", {"from": "a", "to": "b"})])
    # the named-query path, not the spliced one: keeps the name in any error
    assert "-e" not in calls[0]
    assert "link_to" in calls[0]


def test_change_many_does_nothing_when_there_are_no_steps(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("no subprocess expected")
    )
    client.change_many([])


def test_change_many_runs_the_guard_over_every_step(monkeypatch, tmp_path):
    seen: list[str] = []

    def guard(name, params):
        seen.append(name)
        return params

    client = _client(monkeypatch, tmp_path, guard=guard)
    _record(monkeypatch, [])
    client.change_many(
        [
            ("m.gq", "insert_thing", {"slug": "a", "tags": None, "note": None}),
            ("m.gq", "link_to", {"from": "a", "to": "a"}),
        ]
    )
    assert seen == ["insert_thing", "link_to"]


def test_change_many_persists_what_the_guard_rewrote(monkeypatch, tmp_path):
    # the guard redacts secrets; a batch that composed from the ORIGINAL params
    # would write them anyway
    client = _client(
        monkeypatch, tmp_path, guard=lambda name, p: {**p, "note": "[redacted]"}
    )
    calls: list[list[str]] = []
    _record(monkeypatch, calls)
    client.change_many(
        [
            ("m.gq", "insert_thing", {"slug": "a", "tags": None, "note": "sekrit"}),
            ("m.gq", "insert_thing", {"slug": "b", "tags": None, "note": "sekrit"}),
        ]
    )
    params = calls[0][calls[0].index("--params") + 1]
    assert "sekrit" not in params
    assert params.count("[redacted]") == 2


def test_change_many_composes_against_the_real_witan_mutations():
    # guards against mutations.gq drifting into a shape the splicer mishandles
    queries = Path(__file__).parents[3] / "mcp" / "servers" / "witan" / "queries"
    if not queries.exists():  # witan-core is installable on its own
        pytest.skip("witan queries not present")
    now = "2026-08-05T00:00:00Z"
    source, params = og.compose_batch(
        [
            (
                "mutations.gq",
                "insert_memory",
                {
                    "slug": "mem-1",
                    "kind": "pattern",
                    "title": "t",
                    "content": "c",
                    "repo": None,
                    "language": None,
                    "category": None,
                    "severity": None,
                    "author": "a",
                    "tags": ["x"],
                    "symbol_refs": None,
                    "confidence": None,
                    "created_at": now,
                    "updated_at": now,
                },
            ),
            (
                "mutations.gq",
                "insert_topic",
                {"slug": "tp-x", "name": "x", "kind": "topic", "created_at": now},
            ),
            ("mutations.gq", "link_tagged", {"from": "mem-1", "to": "tp-x"}),
        ],
        lambda f: (queries / f).read_text(),
    )
    decls, body = source.split(")", 1)[0], source.split("{", 1)[1]
    assert set(og._PARAM_REF_RE.findall(body)) <= set(og._PARAM_REF_RE.findall(decls))
    assert set(og._PARAM_REF_RE.findall(decls)) == set(params)
    assert source.count("insert ") == 3
