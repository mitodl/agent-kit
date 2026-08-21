"""A failed index has to report at least as much as a successful one.

The run you most need numbers from was the only one that produced none: on
success `witan code index` prints `scanned=… indexed=… skipped=… symbols=…`,
and on failure the exception replaced that line entirely. Attributing the CI
indexer's `ol-infrastructure` timeout therefore needed a traceback line number,
a source read to find the batch constant, row counts from three graphs and a
live timing experiment — nearly all of which the numbers below answer directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from witan_code import cli as cli_module
from witan_code import indexer


def _stats(**kwargs) -> indexer.IndexStats:
    return indexer.IndexStats(**kwargs)


# ── the exception carries the sizes ────────────────────────────────


def test_a_write_failure_keeps_the_partial_stats():
    stats = _stats(scanned=657, indexed=165, skipped=492, symbols=4102)

    with pytest.raises(indexer.IndexFailed) as raised:
        with indexer._write_phase("delete of stale rows", stats, statements=330):
            raise RuntimeError("timed out")

    assert raised.value.stats is stats
    assert raised.value.stats.scanned == 657


def test_the_message_names_the_phase_and_what_it_was_working_with():
    with pytest.raises(indexer.IndexFailed) as raised:
        with indexer._write_phase(
            "delete of stale rows", _stats(), statements=330, chunk_size=128
        ):
            raise RuntimeError("mutate failed: timed out")

    message = str(raised.value)
    assert message.startswith("delete of stale rows failed after ")
    assert "statements=330" in message
    assert "chunk_size=128" in message
    assert "mutate failed: timed out" in message


def test_the_real_error_stays_reachable():
    """The wrapper adds sizes; it must not cost anyone the original."""
    cause = RuntimeError("timed out")

    with pytest.raises(indexer.IndexFailed) as raised:
        with indexer._write_phase("load of nodes and edges", _stats(), records=9000):
            raise cause

    assert raised.value.__cause__ is cause


def test_elapsed_is_measured_not_guessed(monkeypatch):
    """Elapsed is what says whether a client budget was reached or something
    died early — a server-side cutoff and a client timeout both arrive as a
    failed write, and only the elapsed time separates them."""
    clock = {"t": 10.0}
    monkeypatch.setattr(indexer.time, "monotonic", lambda: clock["t"])

    with pytest.raises(indexer.IndexFailed) as raised:
        with indexer._write_phase("delete of stale rows", _stats()):
            clock["t"] += 181.3
            raise RuntimeError("timed out")

    assert raised.value.elapsed == pytest.approx(181.3)
    assert "failed after 181.3s" in str(raised.value)


def test_a_successful_phase_is_transparent():
    with indexer._write_phase("load of nodes and edges", _stats(), records=1):
        pass


# ── the CLI prints them ────────────────────────────────────────────


def test_the_cli_prints_the_partial_summary_before_re_raising(monkeypatch, capsys):
    stats = _stats(scanned=657, indexed=165, skipped=492, symbols=4102, purged=3)

    def failing_index(path, *, force=False, **kwargs):
        raise indexer.IndexFailed(
            "delete of stale rows",
            RuntimeError("timed out after 181.0s"),
            stats=stats,
            elapsed=181.0,
            detail={"statements": 330, "chunk_size": 128},
        )

    monkeypatch.setattr(indexer, "index_path", failing_index)

    with pytest.raises(indexer.IndexFailed):
        cli_module.index(Path("."))

    captured = capsys.readouterr()
    # every number the successful path would have printed
    assert "scanned=657" in captured.out
    assert "indexed=165" in captured.out
    assert "symbols=4102" in captured.out
    assert "purged=3" in captured.out
    # plus what failed, on stderr where the sweep script already redirects
    assert "failed in delete of stale rows" in captured.err
    assert "statements=330" in captured.err


def test_the_failure_still_propagates(monkeypatch):
    """Printing the summary must not turn a failed index into a successful one:
    the sweep counts a repo as indexed by exit status alone."""

    def failing_index(path, *, force=False, **kwargs):
        raise indexer.IndexFailed(
            "load of content hashes",
            RuntimeError("boom"),
            stats=_stats(),
            elapsed=1.0,
            detail={},
        )

    monkeypatch.setattr(indexer, "index_path", failing_index)

    with pytest.raises(indexer.IndexFailed):
        cli_module.reindex(Path("."))


def test_a_failure_that_is_not_a_write_phase_is_left_alone(monkeypatch, capsys):
    """Only a write failure has partial stats worth printing. Anything else —
    an unreadable config, a refused branch — has none, and inventing a summary
    of zeros would read as "the repo is empty" rather than "it never started".
    """

    def failing_index(path, *, force=False, **kwargs):
        raise RuntimeError("witan code: refusing to write to the shared view")

    monkeypatch.setattr(indexer, "index_path", failing_index)

    with pytest.raises(RuntimeError, match="refusing to write"):
        cli_module.index(Path("."))

    assert capsys.readouterr().out == ""
