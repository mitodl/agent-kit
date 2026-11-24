"""The ExternalUse bridge node and its queries, against a local omnigraph.

ExternalUse ships ahead of the indexer that writes it, so these tests write
rows by hand in the shape that writer will use: one per use of a name imported
from another repo's package namespace.
"""

import pytest

from .conftest import requires_stack

REPO_A = "https://github.com/test/repo-a"
REPO_B = "https://github.com/test/repo-b"
NOW = "2026-11-24T00:00:00Z"
MODULE_PATH = "mitol.common.utils.datetime.now_in_utc"
PARENT_PACKAGE = "mitol.common.utils.now_in_utc"


def _use(repo, file, line, target, symbol_id=None):
    return {
        "slug": f"{repo}|{file}|{line}|{target}",
        "repo": repo,
        "file": file,
        "line": line,
        "symbol_id": symbol_id,
        "target": target,
        "language": "python",
        "indexed_at": NOW,
    }


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import store as store_module
    from witan_code.graph import OmnigraphClient

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    cfg = cfg_mod.load()
    store_module.ensure_bridge_store(cfg)
    return OmnigraphClient(
        str(cfg_mod.bridge_store_path(cfg.code_dir)), cfg.queries_dir
    )


def _lookup(client, targets):
    rows = client.read("bridge.gq", "external_uses_by_targets", {"targets": targets})
    return [(r["repo"], r["file"], r["line"], r["target"]) for r in rows]


@requires_stack
def test_uses_are_found_by_any_name_a_definition_is_imported_as(bridge):
    """A reader asks for the module path and the parent package's re-export
    in one read, and gets every repo's uses of either, and nothing else."""
    bridge.change(
        "bridge.gq",
        "insert_external_use",
        _use(REPO_A, "courses/api.py", 3, MODULE_PATH, f"{REPO_A}#courses/api.py::f"),
    )
    bridge.load(
        [
            {
                "type": "ExternalUse",
                "data": _use(REPO_A, "b2b/api.py", 7, PARENT_PACKAGE),
            },
            {
                "type": "ExternalUse",
                "data": _use(REPO_B, "main/utils.py", 1, MODULE_PATH),
            },
            {
                "type": "ExternalUse",
                "data": _use(REPO_B, "main/models.py", 2, "mitol.common.models.Base"),
            },
        ]
    )

    assert _lookup(bridge, [MODULE_PATH, PARENT_PACKAGE]) == [
        (REPO_A, "b2b/api.py", 7, PARENT_PACKAGE),
        (REPO_A, "courses/api.py", 3, MODULE_PATH),
        (REPO_B, "main/utils.py", 1, MODULE_PATH),
    ]
    [row] = bridge.read(
        "bridge.gq", "external_uses_by_targets", {"targets": [PARENT_PACKAGE]}
    )
    assert row["symbol_id"] is None
    assert row["language"] == "python"
    assert _lookup(bridge, ["mitol.common.nope"]) == []


@requires_stack
def test_a_file_purge_drops_only_that_files_uses_in_that_repo(bridge):
    """A reindex replaces the files it re-parsed. The same path in another
    repo, and the repo's other files, keep their rows."""
    bridge.load(
        [
            {
                "type": "ExternalUse",
                "data": _use(REPO_A, "main/utils.py", 1, MODULE_PATH),
            },
            {
                "type": "ExternalUse",
                "data": _use(REPO_A, "main/utils.py", 9, MODULE_PATH),
            },
            {"type": "ExternalUse", "data": _use(REPO_A, "other.py", 4, MODULE_PATH)},
            {
                "type": "ExternalUse",
                "data": _use(REPO_B, "main/utils.py", 1, MODULE_PATH),
            },
        ]
    )

    bridge.change(
        "bridge.gq",
        "delete_external_uses_in_file",
        {"repo": REPO_A, "file": "main/utils.py"},
    )

    assert _lookup(bridge, [MODULE_PATH]) == [
        (REPO_A, "other.py", 4, MODULE_PATH),
        (REPO_B, "main/utils.py", 1, MODULE_PATH),
    ]
