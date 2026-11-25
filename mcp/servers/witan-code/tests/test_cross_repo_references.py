"""References on a shared library's definition reach its uses in other repos.

Two checkouts on one local store: an ol-django-style library, whose
`mitol.common.utils` package star-re-exports its `datetime` and `collections`
submodules but not `helpers`, and an app that imports from it every way the
app repos do. The indexer records each
use of a `mitol.*` name the app loads (``bridge.ExternalUse``), and
``code_find_references`` on the library's definition returns them.
"""

from __future__ import annotations

import pytest

from .conftest import git_checkout, requires_stack, tool_fn

LIB = "https://github.com/test/ol-django"
APP = "https://github.com/test/app"

LIB_FILES = {
    # An explicit re-export.
    "src/common/mitol/common/__init__.py": "from .models import TimestampedModel\n",
    # Star re-exports, relative and absolute, of two of the three submodules.
    "src/common/mitol/common/utils/__init__.py": (
        "from .datetime import *  # noqa: F403\n"
        "from mitol.common.utils.collections import *  # noqa: F403\n"
    ),
    "src/common/mitol/common/utils/datetime.py": "def now_in_utc():\n    return 1\n",
    # ol-django defines `max_or_none` in both; only the first is re-exported.
    "src/common/mitol/common/utils/collections.py": (
        "def max_or_none(values):\n    return max(values, default=None)\n"
    ),
    "src/common/mitol/common/utils/helpers.py": (
        "def max_or_none(values):\n    return max(values) if values else None\n"
    ),
    "src/common/mitol/common/models.py": "class TimestampedModel:\n    pass\n",
    # The library's own use of its module is an in-repo edge, not a use from
    # another repo.
    "src/common/mitol/common/factories.py": (
        "from mitol.common.utils.datetime import now_in_utc\n"
        "\n\n"
        "def stamp():\n"
        "    return now_in_utc()\n"
    ),
}

APP_FILES = {
    # Through the parent package's re-export; a base class; two uses on one line.
    "courses/models.py": (
        "from mitol.common.models import TimestampedModel\n"
        "from mitol.common.utils import now_in_utc\n"
        "\n\n"
        "class Course(TimestampedModel):\n"
        "    def stamp(self):\n"
        "        return now_in_utc() - now_in_utc()\n"
    ),
    # By the full module path, passed as a value rather than called.
    "courses/api.py": (
        "from mitol.common.utils.datetime import now_in_utc\n"
        "\n\n"
        "def created(default=now_in_utc):\n"
        "    return default()\n"
    ),
    # The package's name for each `max_or_none`, and the module path.
    "courses/stats.py": (
        "from mitol.common.utils import max_or_none\n"
        "from mitol.common.utils.helpers import max_or_none as helper_max\n"
        "\n\n"
        "def best(values):\n"
        "    return max_or_none(values)\n"
        "\n\n"
        "def worst(values):\n"
        "    return helper_max(values)\n"
    ),
    # Through the package's explicit re-export.
    "courses/runs.py": (
        "from mitol.common import TimestampedModel\n"
        "\n\n"
        "class Run(TimestampedModel):\n"
        "    pass\n"
    ),
    # `from mitol import common` matches no package-consumer binding, so only
    # its uses know this file.
    "courses/legacy.py": (
        "from mitol import common\n"
        "\n\n"
        "def created():\n"
        "    return common.utils.now_in_utc()\n"
    ),
}

NOW_IN_UTC = f"{LIB}#src/common/mitol/common/utils/datetime.py::now_in_utc"
TIMESTAMPED_MODEL = f"{LIB}#src/common/mitol/common/models.py::TimestampedModel"
UTILS = f"{LIB}#src/common/mitol/common/utils"


@pytest.fixture
def stores(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    cfg = cfg_mod.load()
    lib = git_checkout(tmp_path / "lib", remote=LIB, files=LIB_FILES)
    app = git_checkout(tmp_path / "app", remote=APP, files=APP_FILES)
    for path, repo in ((lib, LIB), (app, APP)):
        indexer.index_path(path, repo_override=repo, config=cfg)
    before = srv.cfg
    srv.use_config(cfg, follow_checkout=False)
    yield cfg, app, lib
    srv.use_config(before)


def _references(symbol_id: str) -> list[tuple[str, str, int, str]]:
    from witan_code import server as srv

    rows = tool_fn(srv.code_find_references)(symbol_id)
    return sorted(
        (r["repo"], r["file_id"].split("#", 1)[1], r["start_line"], r["qualified_name"])
        for r in rows
    )


@requires_stack
def test_references_on_a_library_definition_include_its_uses_in_another_repo(
    stores,
):
    assert _references(NOW_IN_UTC) == [
        (APP, "courses/api.py", 4, "created"),
        (APP, "courses/legacy.py", 5, "created"),
        (APP, "courses/models.py", 7, "Course.stamp"),
        (LIB, "src/common/mitol/common/factories.py", 4, "stamp"),
    ]
    assert _references(TIMESTAMPED_MODEL) == [
        (APP, "courses/models.py", 5, "Course"),
        (APP, "courses/runs.py", 4, "Run"),
    ]


@requires_stack
def test_the_package_name_matches_only_a_definition_the_package_re_exports(
    stores,
):
    """`mitol.common.utils.max_or_none` is `collections`' (star-imported by
    the package), not `helpers`' (which it does not import)."""
    assert _references(f"{UTILS}/collections.py::max_or_none") == [
        (APP, "courses/stats.py", 6, "best"),
    ]
    assert _references(f"{UTILS}/helpers.py::max_or_none") == [
        (APP, "courses/stats.py", 10, "worst"),
    ]


@requires_stack
def test_editing_the_submodule_alone_keeps_the_package_re_export(stores):
    """Reindexing `collections.py` drops its symbols and the package's edges
    into them; the relink re-parses the `__init__` that names it."""
    from witan_code import indexer

    cfg, _, lib = stores
    collections = lib / "src/common/mitol/common/utils/collections.py"
    collections.write_text(collections.read_text() + "\n\ndef added():\n    pass\n")
    indexer.index_path(collections, repo_override=LIB, config=cfg)

    assert _references(f"{UTILS}/collections.py::max_or_none") == [
        (APP, "courses/stats.py", 6, "best"),
    ]


@requires_stack
def test_a_reindex_replaces_a_files_uses_and_drops_a_deleted_files(stores):
    """The deleted file carries no package-consumer binding, so its uses are
    found for the purge from the stored uses themselves."""
    from witan_code import indexer

    cfg, app, _ = stores
    (app / "courses" / "legacy.py").unlink()
    (app / "courses" / "api.py").write_text(
        "from mitol.common.utils.datetime import now_in_utc\n\n\nx = 1\n"
    )
    indexer.index_path(app, repo_override=APP, config=cfg)

    assert [r for r in _references(NOW_IN_UTC) if r[0] == APP] == [
        (APP, "courses/models.py", 7, "Course.stamp"),
    ]


@requires_stack
def test_a_server_without_the_use_files_query_still_replaces_a_files_uses(
    stores, monkeypatch
):
    """Through a deployed witan older than this release the read of the stored
    use files fails. The bridge write still goes through, deleting uses in
    every re-parsed file instead."""
    from witan_code import indexer
    from witan_code.graph import OmnigraphClient

    read = OmnigraphClient.read

    def without_use_files(self, query_file, query_name, params):
        if query_name == "external_use_files":
            raise KeyError(f"query {query_name!r} not found")
        return read(self, query_file, query_name, params)

    monkeypatch.setattr(OmnigraphClient, "read", without_use_files)
    cfg, app, _ = stores
    (app / "courses" / "api.py").write_text(
        "from mitol.common.utils.datetime import now_in_utc\n\n\nx = 1\n"
    )
    stats = indexer.index_path(app, repo_override=APP, config=cfg)

    assert not stats.bridge_failed
    assert (APP, "courses/api.py", 4, "created") not in _references(NOW_IN_UTC)


@requires_stack
def test_references_keep_the_in_repo_rows_when_the_bridge_read_fails(
    stores, monkeypatch
):
    """A bridge without ExternalUse (not yet re-indexed after an upgrade, or a
    deployment older than this release) fails the uses read; the definition
    repo's own references still come back."""
    from witan_code.graph import OmnigraphClient

    read = OmnigraphClient.read

    def without_uses(self, query_file, query_name, params):
        if query_name == "external_uses_by_targets":
            raise RuntimeError("no node type ExternalUse")
        return read(self, query_file, query_name, params)

    monkeypatch.setattr(OmnigraphClient, "read", without_uses)
    assert _references(NOW_IN_UTC) == [
        (LIB, "src/common/mitol/common/factories.py", 4, "stamp"),
    ]
