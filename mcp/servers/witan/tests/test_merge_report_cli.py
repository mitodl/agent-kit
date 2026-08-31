"""What `witan migrate merge` actually prints about whether all of it arrived.

The accounting itself is exercised in `test_merge_report`; this is the surface
the person migrating meets. They are the reason the feature exists — once the
target is the deployed graph they cannot export it to check by hand — so the
wording and the exit code are part of the deliverable, not decoration.
"""

from __future__ import annotations

import pytest


def _capture(monkeypatch):
    """Collect what the merge prints, in order."""
    from witan.cli import _common

    printed: list[str] = []
    monkeypatch.setattr(
        _common.console,
        "print",
        lambda *a, **kw: printed.append(str(a[0]) if a else ""),
    )
    return printed


class _StubProvider:
    """A merge destination returning a canned result, so the rendering is
    tested without a store.

    ``client.graph_uri`` is what ``_destination_key`` reads to name this
    destination in the watermark file.
    """

    remote_url = None

    class client:  # noqa: N801 - matches the attribute the CLI reads
        graph_uri = "/tmp/target.omni"

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def merge_store(self, source, **kwargs):
        if self._error is not None:
            raise self._error
        return self._result


@pytest.fixture
def stub_source(tmp_path):
    """A `.jsonl` source, which is passed through rather than exported — a
    store URI would shell out to omnigraph and never reach the rendering."""
    path = tmp_path / "source.jsonl"
    path.write_text('{"type": "Memory", "data": {"slug": "mem-x-aaaaaa"}}\n')
    return str(path)


def test_a_complete_merge_prints_the_arithmetic_it_was_verified_from(
    monkeypatch, stub_source
):
    """Acceptance 1, at the surface the user meets it.

    The verdict shows its working. A bare "verified" the reader cannot check is
    worth less than the numbers it was computed from — especially here, where
    the whole point is that they have no second way to look.
    """
    from witan.cli import migrate

    provider = _StubProvider(
        {
            "merged": True,
            "target": "https://witan.example/mcp",
            "decisions": [],
            "added": 2,
            "updated": 1,
            "kept_target": 1,
            "diverged": 0,
            "passthrough": 2,
            "duplicate_slugs": 0,
            "source_rows": 6,
            "rows_loaded": 5,
            "watermark": None,
        }
    )
    monkeypatch.setattr(migrate, "_srv", lambda: provider)
    printed = _capture(monkeypatch)

    migrate._merge(stub_source, target=None, dry_run=False)

    verdict = [line for line in printed if "Verified" in line]
    assert verdict, printed
    assert "all 6 source row(s) accounted for" in verdict[0]
    assert "2 added + 1 updated + 1 kept + 2 edge/unkeyed" in verdict[0]


def test_an_interrupted_merge_is_reported_as_incomplete(monkeypatch, stub_source):
    """Acceptance 2.

    The interrupt on its own says only that it was interrupted. What decides
    the user's next move is how much landed and whether re-running is safe.
    """
    from witan import merge_report
    from witan.cli import migrate

    interrupt = KeyboardInterrupt()
    merge_report.attach_partial(
        interrupt,
        {
            "target": "https://witan.example/mcp",
            "batches_applied": 2,
            "source_rows": 100,
            "added": 40,
            "updated": 0,
            "kept_target": 0,
            "diverged": 0,
            "passthrough": 0,
            "duplicate_slugs": 0,
            "rows_loaded": 40,
        },
    )
    monkeypatch.setattr(migrate, "_srv", lambda: _StubProvider(error=interrupt))
    printed = _capture(monkeypatch)

    with pytest.raises(SystemExit) as exit_code:
        migrate._merge(stub_source, target=None, dry_run=False)

    assert exit_code.value.code == 130
    report = "\n".join(printed)
    assert "NOT verified" in report
    assert "the source held 100 row(s) and this merge accounts for 40" in report
    assert "60 row(s) never reached a decision" in report
    assert "stopped after 2 batch(es); it did not roll back" in report
    assert "idempotent" in report, "the user needs to know re-running is the remedy"


def test_a_failure_before_any_batch_landed_does_not_add_an_accounting_block(
    monkeypatch, stub_source
):
    """Nothing was applied, so there is nothing to reconcile — and the refusal
    is meant to read as one line, not five with arithmetic under it
    (`test_cli_remote_tool_errors`)."""
    from witan import merge_report
    from witan.cli import migrate

    failure = RuntimeError("cannot merge: source graph has no __manifest")
    merge_report.attach_partial(
        failure,
        {"batches_applied": 0, "source_rows": 100, "added": 0, "rows_loaded": 0},
    )
    monkeypatch.setattr(migrate, "_srv", lambda: _StubProvider(error=failure))
    printed = _capture(monkeypatch)

    with pytest.raises(SystemExit):
        migrate._merge(stub_source, target=None, dry_run=False)

    assert len(printed) == 1
    assert "no __manifest" in printed[0]


def test_a_deployment_that_reports_no_accounting_says_so(monkeypatch, stub_source):
    """Fails soft, like the watermark. A computed zero here would tell someone
    their merge lost every edge row in the source."""
    from witan.cli import migrate

    provider = _StubProvider(
        {
            "merged": True,
            "target": "https://witan.example/mcp",
            "decisions": [],
            "added": 3,
            "updated": 0,
            "kept_target": 0,
            "diverged": 0,
            "rows_loaded": 3,
            "watermark": None,
        }
    )
    monkeypatch.setattr(migrate, "_srv", lambda: provider)
    printed = _capture(monkeypatch)

    migrate._merge(stub_source, target=None, dry_run=False)

    report = "\n".join(printed)
    assert "does not report merge accounting" in report
    assert "NOT verified" not in report, "cannot-tell must not read as a failure"


def test_a_dry_run_verdict_is_conditional_not_a_claim_about_the_graph(
    monkeypatch, stub_source
):
    """A dry run wrote nothing, so "accounted for" would be a claim about a
    graph it never touched."""
    from witan.cli import migrate

    provider = _StubProvider(
        {
            "dry_run": True,
            "target": "https://witan.example/mcp",
            "decisions": [],
            "added": 4,
            "updated": 0,
            "kept_target": 0,
            "diverged": 0,
            "passthrough": 0,
            "duplicate_slugs": 0,
            "source_rows": 4,
            "rows_loaded": 0,
            "watermark": None,
        }
    )
    monkeypatch.setattr(migrate, "_srv", lambda: provider)
    printed = _capture(monkeypatch)

    migrate._merge(stub_source, target=None, dry_run=True)

    verdict = [line for line in printed if "Verified" in line]
    assert verdict, printed
    assert "would be accounted for" in verdict[0]
