"""The CLI transport passes a query's params, and a composed batch's source, as
files, so neither is bounded by Linux's 128 KiB per-argv-element cap.

As ``--params`` a task with a 200 KiB description failed with ``Argument list
too long`` before omnigraph ever ran, and so did a composed batch of 128
``merge_task`` statements as ``-e``. Against the real binary, because the
point is that ``--params-file`` and ``--query <path>`` are read the way the
inline forms were.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from witan_core.omnigraph import OmnigraphClient

_REQUIRED = os.environ.get("WITAN_REQUIRE_OMNIGRAPH", "") not in ("", "0", "false")
pytestmark = pytest.mark.skipif(
    shutil.which("omnigraph") is None and not _REQUIRED,
    reason="omnigraph binary not on PATH (set WITAN_REQUIRE_OMNIGRAPH=1 to require it)",
)

#: Past the 128 KiB a single argv element may hold.
_BIG = "x" * (200 * 1024)

_SCHEMA = """
node Note {
    slug: String @key
    body: String
}
"""

_MUTATIONS = """
query insert_note($slug: String, $body: String) {
    insert Note { slug: $slug, body: $body }
}
"""

_READS = """
query note_by_body($body: String) {
    match {
        $n: Note { body: $body }
    }
    return { $n.slug, $n.body }
}
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    import subprocess

    queries = tmp_path / "queries"
    queries.mkdir()
    (queries / "mutations.gq").write_text(_MUTATIONS)
    (queries / "read.gq").write_text(_READS)
    schema = tmp_path / "schema.pg"
    schema.write_text(_SCHEMA)
    store = tmp_path / "notes.omni"
    subprocess.run(
        ["omnigraph", "init", "--schema", str(schema), str(store)],
        check=True,
        capture_output=True,
    )
    # A TMPDIR of the test's own, to see the payload files are cleaned up.
    payloads = tmp_path / "tmp"
    payloads.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(payloads))
    return OmnigraphClient(str(store), queries)


def _leftovers() -> list[Path]:
    return list(Path(tempfile.gettempdir()).glob("witan-omnigraph-*"))


def test_a_mutation_and_a_query_with_params_past_the_argv_cap(client):
    client.change("mutations.gq", "insert_note", {"slug": "n-1", "body": _BIG})

    rows = client.read("read.gq", "note_by_body", {"body": _BIG})

    assert rows == [{"slug": "n-1", "body": _BIG}]
    assert _leftovers() == []


def test_a_composed_batch_past_the_argv_cap(client):
    client.change_many(
        [
            ("mutations.gq", "insert_note", {"slug": "n-1", "body": _BIG}),
            ("mutations.gq", "insert_note", {"slug": "n-2", "body": _BIG + "y"}),
        ]
    )

    assert client.read("read.gq", "note_by_body", {"body": _BIG + "y"}) == [
        {"slug": "n-2", "body": _BIG + "y"}
    ]
    assert _leftovers() == []
