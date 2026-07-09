"""Cross-platform detached-Popen wrapper: verify the right kwarg lands per platform.

Mirrors mcp/servers/witan-code/tests/test_detach.py's coverage of the
equivalent witan-code function — the two are deliberately duplicated (no
cross-package import, see _detach.py's docstring), so their tests are
duplicated too.
"""

import subprocess

from witan import _detach


def test_popen_detached_uses_start_new_session_on_posix(monkeypatch):
    monkeypatch.setattr(_detach.sys, "platform", "linux")
    captured = {}
    monkeypatch.setattr(
        _detach.subprocess,
        "Popen",
        lambda args, **kwargs: captured.update(kwargs) or "proc",
    )

    result = _detach.popen_detached(["echo", "hi"])

    assert result == "proc"
    assert captured.get("start_new_session") is True
    assert "creationflags" not in captured


def test_popen_detached_uses_creationflags_on_windows(monkeypatch):
    monkeypatch.setattr(_detach.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    captured = {}
    monkeypatch.setattr(
        _detach.subprocess,
        "Popen",
        lambda args, **kwargs: captured.update(kwargs) or "proc",
    )

    result = _detach.popen_detached(["echo", "hi"])

    assert result == "proc"
    assert captured.get("creationflags") == 0x200 | 0x8
    assert "start_new_session" not in captured
