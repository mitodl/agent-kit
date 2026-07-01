"""Platform-specific config-directory helpers. Moved verbatim from ``witan/setup.py``."""

from __future__ import annotations

import platform
from pathlib import Path


def vscode_user_dir() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    return Path.home() / ".config" / "Code" / "User"
