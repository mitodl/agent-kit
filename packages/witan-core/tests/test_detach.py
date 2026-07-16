import subprocess
import sys

from witan_core import _detach


def test_popen_detached_uses_start_new_session_on_posix(monkeypatch):
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "proc"

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = _detach.popen_detached(["echo", "hi"])

    assert result == "proc"
    assert captured["kwargs"]["start_new_session"] is True
    assert "creationflags" not in captured["kwargs"]


def test_popen_detached_uses_creationflags_on_windows(monkeypatch):
    captured = {}

    def fake_popen(args, **kwargs):
        captured["kwargs"] = kwargs
        return "proc"

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = _detach.popen_detached(["echo", "hi"])

    assert result == "proc"
    assert captured["kwargs"]["creationflags"] == 0x208
    assert "start_new_session" not in captured["kwargs"]
