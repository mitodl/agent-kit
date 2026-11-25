"""References on a shared library's definition reach its uses in other repos.

Two checkouts on one local store: an ol-django-style library, whose
`mitol.common.utils` package star-re-exports its `datetime` submodule, and an
app that imports from it every way the app repos do. The indexer records each
use of a `mitol.*` name the app loads (``bridge.ExternalUse``), and
``code_find_references`` on the library's definition returns them.
"""

from __future__ import annotations

import pytest

from .conftest import git_checkout, requires_stack, tool_fn

LIB = "https://github.com/test/ol-django"
APP = "https://github.com/test/app"

LIB_FILES = {
    "src/common/mitol/common/__init__.py": "",
    "src/common/mitol/common/utils/__init__.py": "from .datetime import *  # noqa: F403\n",
    "src/common/mitol/common/utils/datetime.py": "def now_in_utc():\n    return 1\n",
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
    yield cfg, app
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
    ]


@requires_stack
def test_a_reindex_replaces_a_files_uses_and_drops_a_deleted_files(stores):
    """The deleted file carries no package-consumer binding, so its uses are
    found for the purge from the stored uses themselves."""
    from witan_code import indexer

    cfg, app = stores
    (app / "courses" / "legacy.py").unlink()
    (app / "courses" / "api.py").write_text(
        "from mitol.common.utils.datetime import now_in_utc\n\n\nx = 1\n"
    )
    indexer.index_path(app, repo_override=APP, config=cfg)

    assert [r for r in _references(NOW_IN_UTC) if r[0] == APP] == [
        (APP, "courses/models.py", 7, "Course.stamp"),
    ]
