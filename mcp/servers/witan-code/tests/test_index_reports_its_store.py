"""`witan code index` has to say which graph it wrote.

Memory and code graphs are routed by SEPARATE settings, so a target whose
memory is deployed can still index onto the laptop — three non-maintainers were
in exactly that state on production. The one command whose entire output is
about where symbols went was silent about which store received them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from witan_code import cli as cli_module
from witan_code import indexer


def test_a_successful_index_names_the_store(monkeypatch, capsys):
    stats = indexer.IndexStats(
        scanned=12,
        indexed=12,
        symbols=88,
        store="/home/dev/.local/share/witan-code/x.omni",
    )
    monkeypatch.setattr(indexer, "index_path", lambda path, *, force=False, **k: stats)

    cli_module.index(Path("."))

    assert "/home/dev/.local/share/witan-code/x.omni" in capsys.readouterr().out


def test_a_shared_store_is_reported_the_same_way(monkeypatch, capsys):
    """Printed for local and shared alike. A line that appears only in the bad
    case is a line nobody has learned to look for — and reading it is how "where
    did this go" gets answered without knowing the setting exists."""
    stats = indexer.IndexStats(
        scanned=12, store="github.com/mitodl/agent-kit (via https://witan.example/mcp)"
    )
    monkeypatch.setattr(indexer, "index_path", lambda path, *, force=False, **k: stats)

    cli_module.index(Path("."))

    assert "https://witan.example/mcp" in capsys.readouterr().out


def test_the_branch_view_is_named_when_there_is_one(monkeypatch, capsys):
    """On a shared graph the view is per writer per git branch, so the store
    alone does not identify what a reader has to look at."""
    stats = indexer.IndexStats(store="s", branch="act-0eb-feature-x")
    monkeypatch.setattr(indexer, "index_path", lambda path, *, force=False, **k: stats)

    cli_module.index(Path("."))

    assert "branch=act-0eb-feature-x" in capsys.readouterr().out


def test_the_default_branch_prints_no_branch_field(monkeypatch, capsys):
    stats = indexer.IndexStats(store="s")
    monkeypatch.setattr(indexer, "index_path", lambda path, *, force=False, **k: stats)

    cli_module.index(Path("."))

    assert "branch=" not in capsys.readouterr().out


def test_a_failed_index_still_names_the_store(monkeypatch, capsys):
    """The failing run is the one where "which graph was this?" is the open
    question, so the destination is carried on the stats rather than recomputed
    by the printer."""
    stats = indexer.IndexStats(scanned=657, store="https://witan.example/mcp")

    def failing_index(path, *, force=False, **kwargs):
        raise indexer.IndexFailed(
            "load of nodes and edges",
            RuntimeError("timed out"),
            stats=stats,
            elapsed=181.0,
            detail={"records": 9000},
        )

    monkeypatch.setattr(indexer, "index_path", failing_index)

    with pytest.raises(indexer.IndexFailed):
        cli_module.index(Path("."))

    assert "https://witan.example/mcp" in capsys.readouterr().out
