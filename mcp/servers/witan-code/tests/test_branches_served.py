"""witan-code's branch views against a real omnigraph-server, over HTTP only.

A view's reads, writes and commit list each carry the view's branch. Against a
deployed server those have to go over HTTP: the 0.14 CLI refuses a plain-http
server target, which is what the deployed data tier is. So these fail the
moment anything here shells out, and assert on where each write landed rather
than on what was sent, because a branch the server ignored would still look
right on the wire.

Needs ``omnigraph-server``; see ``testsupport/omnigraph_server.py``.
"""

from __future__ import annotations

import time

import pytest
from testsupport.omnigraph_server import requires_omnigraph_server, serve

from witan_code import config as cfg_module
from witan_code.graph import OmnigraphClient
from witan_core import omnigraph as og

pytestmark = requires_omnigraph_server

GRAPH = "code-served"
VIEW = "act-alice/feature_x"


def _file(slug: str) -> dict:
    return {
        "id": slug,
        "repo": "github.com/test/repo",
        "path": slug,
        "language": "python",
        "content_hash": "h",
        "indexed_at": "2026-10-28T00:00:00Z",
    }


def _slugs(client: OmnigraphClient) -> set[str]:
    return {row["slug"] for row in client.read("code_read.gq", "all_file_hashes", {})}


@pytest.fixture
def served(tmp_path, monkeypatch):
    def no_subprocess(*args, **kwargs):
        raise AssertionError(f"shelled out against a served graph: {args}")

    with serve(tmp_path, cfg_module._SCHEMA_FILE, GRAPH) as url:
        monkeypatch.setattr(og.subprocess, "run", no_subprocess)

        def client(branch: str | None = None) -> OmnigraphClient:
            return OmnigraphClient(
                url, cfg_module._QUERIES_DIR, branch=branch, graph_id=GRAPH
            )

        yield client


def test_a_view_reads_writes_and_dates_its_own_branch_over_http(served):
    main, view = served(), served(VIEW)
    view.ensure_branch()

    main.change("code_mutations.gq", "insert_file", _file("on-main"))
    view.change("code_mutations.gq", "insert_file", _file("on-view"))
    view.change_many(
        [
            ("code_mutations.gq", "insert_file", _file("batched-1")),
            ("code_mutations.gq", "insert_file", _file("batched-2")),
        ]
    )

    assert _slugs(main) == {"on-main"}
    assert _slugs(view) == {"on-view", "batched-1", "batched-2"}

    written = view.branch_last_write(VIEW)
    assert written is not None
    assert abs(written - time.time()) < 300


def test_a_view_with_no_writes_of_its_own_has_no_last_write(served):
    served().change("code_mutations.gq", "insert_file", _file("on-main"))
    empty = served("act-bob/untouched")
    empty.ensure_branch()

    assert served().branch_last_write("act-bob/untouched") is None


def test_a_missing_view_is_refused_at_once_not_read_from_main(served, monkeypatch):
    """0.13 answers a branch that does not exist with 404 `not_found`. Waiting
    cannot create it, so it fails on the first attempt (the sleep would only
    run between retries) instead of spending the call budget."""
    main = served()
    main.change("code_mutations.gq", "insert_file", _file("on-main"))

    def no_retry(seconds):
        raise AssertionError(f"retried a missing branch (slept {seconds}s)")

    missing = served("act-alice/never_created")

    with monkeypatch.context() as patch:
        patch.setattr(og.time, "sleep", no_retry)
        with pytest.raises(RuntimeError, match="not found"):
            missing.read("code_read.gq", "all_file_hashes", {})
        with pytest.raises(RuntimeError, match="not found"):
            missing.change("code_mutations.gq", "insert_file", _file("lost"))
        with pytest.raises(RuntimeError, match="not found"):
            main.commit_list("act-alice/never_created")
    assert _slugs(main) == {"on-main"}
