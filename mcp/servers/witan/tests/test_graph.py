"""witan-subclass-specific OmnigraphClient tests.

The generic base machinery (_find_binary lookup order, OCC conflict surfacing,
admission-cap backoff) is covered in packages/witan-core/tests/test_omnigraph.py.
Here we only assert witan's own subclass tail: the setup-hint in the
binary-not-found message. (apply_schema is exercised against a real store in
test_migrate.py.)
"""

import shutil
from pathlib import Path

import pytest

from witan.graph import OmnigraphClient


def test_find_binary_message_names_witan_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="witan setup"):
        OmnigraphClient._find_binary()
