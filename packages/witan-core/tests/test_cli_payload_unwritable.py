"""A CLI payload that cannot be written fails as a CLI that could not run.

Every local query and mutation writes its params to ``TMPDIR`` since the
transport stopped passing them as argv, so a full or unwritable ``TMPDIR``
now fails small calls too. Callers that degrade quietly catch
``RuntimeError``, which a bare ``OSError`` would get past.
"""

import errno
import tempfile
from pathlib import Path

import pytest

from witan_core.omnigraph import _cli_payload


def test_an_unusable_tmpdir_is_a_runtime_error_naming_it(tmp_path, monkeypatch):
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("")
    monkeypatch.setattr(tempfile, "tempdir", str(not_a_dir))

    with (
        pytest.raises(RuntimeError, match="TMPDIR") as raised,
        _cli_payload({"slug": "x"}),
    ):
        pass
    assert str(not_a_dir) in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError)


def test_a_failed_write_is_a_runtime_error_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def full(self, *args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Path, "write_text", full)

    with (
        pytest.raises(RuntimeError, match="No space left on device"),
        _cli_payload({"slug": "x"}, "query q() {}"),
    ):
        pass
    assert not list(tmp_path.glob("witan-omnigraph-*"))


def test_an_error_inside_the_call_is_not_rewrapped(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    with pytest.raises(FileNotFoundError), _cli_payload({"slug": "x"}):
        raise FileNotFoundError("from the call, not the payload")
