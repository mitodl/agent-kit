"""File-copy installers for skills and hook/extension scripts.

These return the destination paths they wrote (or would write, under
``dry_run``) rather than printing progress themselves — callers own how to
report that to a user.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .models import SkillSource


def install_skills(
    skills: list[SkillSource],
    dest_dirs: list[Path],
    dry_run: bool,
) -> list[Path]:
    """Copy each skill's SKILL.md into every dest dir (Pi needs two)."""
    dests: list[Path] = []
    for skill in skills:
        for dest_base in dest_dirs:
            dest = dest_base / skill.name / "SKILL.md"
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(skill.skill_md_path, dest)
            dests.append(dest)
    return dests


def install_files(
    src_dir: Path,
    dest_dir: Path,
    *,
    suffix: str,
    dry_run: bool,
    executable: bool = False,
) -> list[Path]:
    """Copy every ``*suffix`` file in ``src_dir`` into ``dest_dir``."""
    if not src_dir.is_dir():
        return []
    dests: list[Path] = []
    for src_file in sorted(src_dir.iterdir()):
        if src_file.suffix != suffix or not src_file.is_file():
            continue
        dest = dest_dir / src_file.name
        if not dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)
            if executable:
                dest.chmod(0o755)
        dests.append(dest)
    return dests
