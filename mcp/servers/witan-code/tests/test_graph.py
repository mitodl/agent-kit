"""witan-code-subclass-specific OmnigraphClient tests.

The generic base machinery (_find_binary lookup order, OCC conflict surfacing,
admission-cap backoff) is covered in packages/witan-core/tests/test_omnigraph.py.
Here we only assert witan-code's own subclass tail: the setup-hint in the
binary-not-found message. (branch ops + bulk load are exercised against a real
store in test_branches.py / test_indexer.py.)
"""

import shutil
from pathlib import Path

import pytest

from witan_code.graph import OmnigraphClient


def test_find_binary_message_names_witan_code_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="witan-code setup"):
        OmnigraphClient._find_binary()
