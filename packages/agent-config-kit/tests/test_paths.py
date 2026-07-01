from pathlib import Path

from agent_config_kit.paths import vscode_user_dir


def test_vscode_user_dir_darwin(monkeypatch, tmp_path):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert (
        vscode_user_dir()
        == tmp_path / "Library" / "Application Support" / "Code" / "User"
    )


def test_vscode_user_dir_linux(monkeypatch, tmp_path):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert vscode_user_dir() == tmp_path / ".config" / "Code" / "User"


def test_vscode_user_dir_windows_uses_appdata_env_var(monkeypatch, tmp_path):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))

    assert vscode_user_dir() == tmp_path / "Roaming" / "Code" / "User"


def test_vscode_user_dir_windows_falls_back_without_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert vscode_user_dir() == tmp_path / "AppData" / "Roaming" / "Code" / "User"
