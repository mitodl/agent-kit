"""File-copy installers for skills and hook/extension scripts.

These return the destination paths they wrote (or would write, under
``dry_run``) rather than printing progress themselves — callers own how to
report that to a user.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .models import SkillSource


def skill_files(skill: SkillSource) -> list[Path]:
    """Every file belonging to a skill, relative to the skill's own
    directory — the full Agent Skills payload (``SKILL.md`` plus whatever
    sits alongside it: ``scripts/``, ``references/``, ``assets/``, or
    anything else). The manifest only ever names a skill's ``SKILL.md``; this
    walk is how the *rest* of its files are discovered, both for copying
    (``install_skills``) and for prune tracking (``prune.py``) — a skill's
    file set can change between two applies without its name or
    ``skill_md_path`` changing, so anything that needs to know "what did this
    skill actually put on disk" must re-derive it from here rather than the
    manifest alone."""
    src_dir = skill.skill_md_path.parent
    return [p.relative_to(src_dir) for p in sorted(src_dir.rglob("*")) if p.is_file()]


def install_skills(
    skills: list[SkillSource],
    dest_dirs: list[Path],
    dry_run: bool,
) -> list[Path]:
    """Copy each skill's full directory into every dest dir (Pi needs two).

    Per the Agent Skills packaging convention, a skill is its ``SKILL.md``
    plus whatever sits alongside it in the same directory — ``scripts/``,
    ``references/``, ``evals/``, or anything else the skill's instructions
    refer to by relative path. Copying only ``SKILL.md`` would silently
    strip those out and break any skill that isn't a single bare file.
    ``shutil.copy2`` preserves each file's mode bits, so executable scripts
    stay executable at the destination.

    Returns every destination file path copied (or that would be copied,
    under ``dry_run``) — not just each skill's ``SKILL.md`` — so drift
    detection (``diff.py``) also catches a partially-installed skill missing
    one of its supporting files.
    """
    dests: list[Path] = []
    for skill in skills:
        src_dir = skill.skill_md_path.parent
        for dest_base in dest_dirs:
            skill_dest_dir = dest_base / skill.name
            for rel in skill_files(skill):
                dest = skill_dest_dir / rel
                if not dry_run:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_dir / rel, dest)
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
