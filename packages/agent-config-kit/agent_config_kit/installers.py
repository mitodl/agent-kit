"""File-copy installers for skills and hook/extension scripts.

These return the destination paths they wrote (or would write, under
``dry_run``) rather than printing progress themselves — callers own how to
report that to a user.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .models import SKILL_NAME_PATTERN, SkillSource


class ConflictingPathError(Exception):
    """Raised when a destination directory can't be created because
    something already occupies that path that isn't a plain directory —
    e.g. a leftover file or a symlink (often dangling, from an older
    symlink-based install scheme). ``exist_ok=True`` on ``Path.mkdir``
    doesn't help here: it only suppresses the error when the existing path
    already resolves to a real directory."""


def _ensure_dest_dir(path: Path, *, force: bool) -> None:
    if path.is_dir():  # also true for a symlink resolving to a directory
        return
    if path.exists() or path.is_symlink():
        if not force:
            raise ConflictingPathError(
                f"{path} already exists and is not a directory "
                "(rerun with --force to replace it)"
            )
        path.unlink()
    path.mkdir(parents=True, exist_ok=True)


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
    manifest alone.

    Raises ``FileNotFoundError`` if ``skill_md_path`` doesn't exist — silently
    yielding an empty list here would otherwise make ``apply``/``validate``
    quietly skip an entire skill instead of surfacing the mistake.
    """
    if not skill.skill_md_path.is_file():
        raise FileNotFoundError(
            f"skill_md_path does not exist or is not a file: {skill.skill_md_path}"
        )
    # Belt-and-suspenders: SkillSource.name is already validated at
    # construction time (models.py), but that validation can't help state
    # loaded straight from an on-disk file (prune.py's state file) or a
    # SkillSource mutated after construction — check again here, at the
    # point a name is actually used to build a filesystem path, since that's
    # the one place path traversal via a crafted name would actually matter.
    if not SKILL_NAME_PATTERN.fullmatch(skill.name):
        raise ValueError(f"unsafe or invalid skill name: {skill.name!r}")
    src_dir = skill.skill_md_path.parent
    return [p.relative_to(src_dir) for p in sorted(src_dir.rglob("*")) if p.is_file()]


def skill_content_hash(skill: SkillSource) -> str:
    """SHA-256 over every file ``skill_files()`` walks for this skill —
    the exact tree ``install_skills`` copies, so this is precisely "would
    this copy differ" (staleness-detection spec S1). Each file's relative
    path is folded into the hash alongside its bytes, not just the
    concatenated content, so two skills that differ only in how their
    bytes are split across files (e.g. moving text from ``SKILL.md`` into
    a new ``scripts/foo.sh``) don't hash identically."""
    src_dir = skill.skill_md_path.parent
    digest = hashlib.sha256()
    for rel in skill_files(skill):
        digest.update(rel.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((src_dir / rel).read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def hook_content_hash(entry_path: Path) -> str:
    """SHA-256 of a plugin hook's own ``entry_path`` file — the file
    ``apply()`` copies verbatim (``shutil.copy2``), so this answers the
    same "would this copy differ" question ``skill_content_hash`` answers
    for a skill (staleness-detection spec S1)."""
    return f"sha256:{hashlib.sha256(entry_path.read_bytes()).hexdigest()}"


def install_skills(
    skills: list[SkillSource],
    dest_dirs: list[Path],
    dry_run: bool,
    *,
    force: bool = False,
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

    Raises ``ConflictingPathError`` if a skill's destination directory is
    already occupied by something that isn't a directory (typically a
    leftover — often dangling — symlink from an older symlink-based install)
    unless ``force`` is set, in which case that path is removed first.
    """
    dests: list[Path] = []
    for skill in skills:
        src_dir = skill.skill_md_path.parent
        rel_files = skill_files(skill)
        for dest_base in dest_dirs:
            skill_dest_dir = dest_base / skill.name
            if not dry_run:
                _ensure_dest_dir(skill_dest_dir, force=force)
            for rel in rel_files:
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
    force: bool = False,
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
            _ensure_dest_dir(dest_dir, force=force)
            shutil.copy2(src_file, dest)
            if executable:
                dest.chmod(0o755)
        dests.append(dest)
    return dests
