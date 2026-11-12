"""code_search_symbol matches docstrings as well as names, names first."""

import dataclasses

import pytest

from .conftest import requires_stack
from .test_server import _fn

# `budget_report` matches "budget" by name, `throttle` only by its docstring,
# and `budget` by both, so one query covers each case.
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


def _names(hits):
    return [h["qualified_name"] for h in hits]


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

    assert _names(_fn(srv.code_search_symbol)("enforce")) == ["throttle"]
    assert _names(_fn(srv.code_search_symbol)("enforce", kind="function")) == [
        "throttle"
    ]
    assert _fn(srv.code_search_symbol)("enforce", kind="class") == []


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

    assert _names(_fn(srv.code_search_symbol)("enforce")) == ["throttle"]


class _RaisingClient:
    graph_uri = "file:///stores/stale.omni"

    def __init__(self, message):
        self.message = message

    def read(self, *_args):
        raise RuntimeError(self.message)


def test_a_missing_docstring_index_yields_no_docstring_rows():
    from witan_code import server

    client = _RaisingClient(
        "omnigraph query failed (exit 1):\ntype error: T27: `search` over "
        "`$s.docstring` needs a full-text index, and `Symbol.docstring` declares "
        "none (bad_request)"
    )

    assert server._docstring_search(client, "search_symbols_docstring", {}) == []


def test_any_other_docstring_search_failure_is_raised():
    from witan_code import server

    client = _RaisingClient("omnigraph query failed (exit 1):\nstorage: not found")

    with pytest.raises(RuntimeError, match="storage: not found"):
        server._docstring_search(client, "search_symbols_docstring", {})
