"""Platform-specific config-directory helpers. Moved verbatim from ``witan/setup.py``."""

from __future__ import annotations

import os
import platform
from pathlib import Path


def vscode_user_dir() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Code" / "User"
    return Path.home() / ".config" / "Code" / "User"
