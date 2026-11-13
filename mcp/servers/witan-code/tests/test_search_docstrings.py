"""code_search_symbol matches docstrings as well as names, names first."""

import dataclasses
import shutil

import pytest

from .conftest import requires_stack
from .test_remote_store import REPO as REMOTE_REPO
from .test_remote_store import in_memory_tier  # noqa: F401 (fixture)
from .test_server import _fn

# `budget_report` matches "budget" by name, `throttle` only by its docstring,
# and `budget` by both, so one query covers each case.
# Queries use words written in lower case here: omnigraph 0.13's analyzer does
# not fold case (0.15's does), and the suite runs on either.
DOC_SAMPLE = '''\
def throttle():
    """Enforce the request budget for each client."""
    return 1


def budget_report():
    return 2


def budget():
    """Return the budget."""
    return 3
'''


@pytest.fixture
def doc_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/docs")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    src = tmp_path / "src"
    src.mkdir()
    (src / "limits.py").write_text(DOC_SAMPLE)
    return src


@pytest.fixture
def srv(monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import server

    monkeypatch.setattr(server, "cfg", cfg_mod.load())
    server._clients.clear()
    return server


def _names(result):
    return [h["qualified_name"] for h in result["symbols"]]


@requires_stack
def test_a_docstring_only_match_is_returned_after_every_name_match(doc_repo, srv):
    _fn(srv.code_reindex)(path=str(doc_repo))

    names = _names(_fn(srv.code_search_symbol)("budget"))

    assert "throttle" in names
    assert names.index("throttle") > max(
        names.index("budget_report"), names.index("budget")
    )
    # A symbol matching on both fields is listed once, as a name match.
    assert names.count("budget") == 1


@requires_stack
def test_a_query_matching_only_a_docstring_finds_the_symbol(doc_repo, srv):
    _fn(srv.code_reindex)(path=str(doc_repo))

    assert _names(_fn(srv.code_search_symbol)("request")) == ["throttle"]
    assert _names(_fn(srv.code_search_symbol)("request", kind="function")) == [
        "throttle"
    ]
    assert _fn(srv.code_search_symbol)("request", kind="class") == {"symbols": []}


@requires_stack
def test_a_store_without_the_docstring_index_gains_it_on_the_next_index(
    doc_repo, srv, tmp_path, monkeypatch
):
    """The migration path for a local store built before `@index` was added."""
    current = srv.cfg.schema_file.read_text()
    old = current.replace("docstring: String? @index", "docstring: String?")
    assert old != current
    old_schema = tmp_path / "old-code-schema.pg"
    old_schema.write_text(old)
    new_cfg = srv.cfg
    monkeypatch.setattr(
        srv, "cfg", dataclasses.replace(new_cfg, schema_file=old_schema)
    )
    _fn(srv.code_reindex)(path=str(doc_repo))

    # On a store without the index the name run still answers. (omnigraph
    # 0.15 refuses the docstring run there; 0.13 scans the field instead.)
    assert "budget_report" in _names(_fn(srv.code_search_symbol)("budget"))

    monkeypatch.setattr(srv, "cfg", new_cfg)
    srv._clients.clear()
    _fn(srv.code_reindex)(path=str(doc_repo))

    result = _fn(srv.code_search_symbol)("request")
    assert _names(result) == ["throttle"]
    assert "note" not in result


class _FakeClient:
    """A store that answers each named query from ``answers``.

    A value that is an exception is raised instead, as the transports raise a
    refusal.
    """

    def __init__(self, answers):
        self.answers = answers

    def read(self, _file, name, _params):
        answer = self.answers[name]
        if isinstance(answer, Exception):
            raise answer
        return [dict(row) for row in answer]


def _row(name, repo="https://github.com/test/a"):
    return {"slug": f"{repo}#m.py::{name}", "qualified_name": name}


T27 = RuntimeError(
    "omnigraph query failed (exit 1):\ntype error: T27: `search` over "
    "`$s.docstring` needs a full-text index, and `Symbol.docstring` declares "
    "none (HTTP 400, bad_request)"
)


def test_a_missing_docstring_index_yields_no_docstring_rows_and_a_reason():
    from witan_code import server

    client = _FakeClient({"search_symbols_docstring": T27})

    rows, reason = server._docstring_search(client, "search_symbols_docstring", {})
    assert rows == []
    assert "no full-text index on Symbol.docstring" in reason


def test_a_store_without_the_docstring_index_says_its_results_are_partial(
    monkeypatch,
):
    """The production case: name matches, plus a note the caller can see."""
    from witan_code import server

    client = _FakeClient(
        {"search_symbols": [_row("a_name")], "search_symbols_docstring": T27}
    )
    monkeypatch.setattr(server, "_resolve_clients", lambda _repo, _branch: [client])

    result = _fn(server.code_search_symbol)("x")

    assert _names(result) == ["a_name"]
    assert result["note"].startswith(
        "docstring search unavailable, so these are name matches only: "
    )
    assert "no full-text index on Symbol.docstring" in result["note"]


def test_a_fan_out_note_counts_the_stores_that_skipped_docstrings(monkeypatch):
    from witan_code import server

    indexed = _FakeClient(
        {
            "search_symbols": [_row("a_name")],
            "search_symbols_docstring": [_row("a_doc")],
        }
    )
    b_repo = "https://github.com/test/b"
    unindexed = _FakeClient(
        {"search_symbols": [_row("b_name", b_repo)], "search_symbols_docstring": T27}
    )
    monkeypatch.setattr(
        server, "_resolve_clients", lambda _repo, _branch: [indexed, unindexed]
    )

    result = _fn(server.code_search_symbol)("x")

    assert _names(result) == ["a_name", "b_name", "a_doc"]
    assert result["note"].startswith(
        "docstring search unavailable in 1 of the 2 stores searched"
    )


def test_any_other_docstring_search_failure_is_raised():
    from witan_code import server

    client = _FakeClient(
        {"search_symbols_docstring": RuntimeError("storage: not found")}
    )

    with pytest.raises(RuntimeError, match="storage: not found"):
        server._docstring_search(client, "search_symbols_docstring", {})


def test_name_matches_from_every_store_precede_docstring_only_ones(monkeypatch):
    from witan_code import server

    a = _FakeClient(
        {
            "search_symbols": [_row("a_name")],
            "search_symbols_docstring": [_row("a_doc")],
        }
    )
    b_repo = "https://github.com/test/b"
    b = _FakeClient(
        {
            "search_symbols": [_row("b_name", b_repo)],
            "search_symbols_docstring": [_row("b_doc", b_repo), _row("b_name", b_repo)],
        }
    )
    monkeypatch.setattr(server, "_resolve_clients", lambda _repo, _branch: [a, b])

    result = _fn(server.code_search_symbol)("x")

    names = _names(result)
    assert sorted(names[:2]) == ["a_name", "b_name"]
    assert sorted(names[2:]) == ["a_doc", "b_doc"]
    assert all(set(h) == {"symbol_id", "qualified_name"} for h in result["symbols"])
    assert set(result) == {"symbols"}


def test_a_failing_store_keeps_the_other_stores_name_matches(monkeypatch):
    from witan_code import server

    healthy = _FakeClient(
        {"search_symbols": [_row("a_name")], "search_symbols_docstring": []}
    )
    broken = RuntimeError("storage: not found")
    failing = _FakeClient(
        {"search_symbols": broken, "search_symbols_docstring": broken}
    )
    monkeypatch.setattr(
        server, "_resolve_clients", lambda _repo, _branch: [healthy, failing]
    )

    assert _names(_fn(server.code_search_symbol)("x")) == ["a_name"]


@requires_stack
def test_a_tier_without_the_docstring_query_still_answers_by_name(
    in_memory_tier,  # noqa: F811 (the imported fixture)
    tmp_path,
    monkeypatch,
):
    """A client on this release reading through a deployed tier on the last one."""
    from witan_code import indexer, ingest
    from witan_code import server as srv
    from witan_code.remote.store import RemoteStoreClient

    session, server_cfg = in_memory_tier
    old_queries = tmp_path / "old-queries"
    shutil.copytree(server_cfg.queries_dir, old_queries)
    gq = old_queries / "code_read.gq"
    text = gq.read_text()
    start = text.index("// ── Full-text symbol search (BM25 on docstring)")
    gq.write_text(text[:start] + text[text.index("// ── File symbols") :])
    old_tier = dataclasses.replace(server_cfg, queries_dir=old_queries)
    monkeypatch.setattr(ingest, "_config", lambda: old_tier)
    src = tmp_path / "src"
    src.mkdir()
    (src / "limits.py").write_text(DOC_SAMPLE)
    indexer.index_path(src, config=old_tier)

    client = RemoteStoreClient(REMOTE_REPO, session)
    rows, reason = srv._docstring_search(
        client, "search_symbols_docstring", {"query": "request"}
    )
    assert rows == []
    assert "predates docstring search" in reason
    assert "budget_report" in _names(
        {"symbols": client.read("code_read.gq", "search_symbols", {"query": "budget"})}
    )
