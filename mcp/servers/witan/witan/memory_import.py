"""Read Claude Code's per-project memory files for ``witan memory import``.

Claude Code keeps one fact per file under
``~/.claude/projects/<project>/memory/*.md``, with YAML frontmatter carrying
``name``, ``description`` and ``metadata.type``. ``MEMORY.md`` beside them is
an index of links to the others, not a memory.

``<project>`` is the session's working directory with every character that is
not a letter or digit replaced by ``-``. That loses information: ``-`` in the
name may have been ``/``, ``.``, ``_`` or ``-`` itself. So the directory is not
decoded but matched, walking the filesystem from ``/`` and keeping every path
whose own encoding equals the name. A match counts only if it is a git
checkout with a remote; anything else is reported as unresolved rather than
guessed at.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml

from witan_core.repo_key import normalise

from . import repo as repo_module

#: Claude Code memory ``type`` → witan memory kind. ``user`` memories describe
#: a person, so the importer leaves them out unless one is named explicitly.
KIND_BY_TYPE = {
    "project": "project_fact",
    "reference": "project_fact",
    "feedback": "lesson",
    "user": "agent_context",
}

#: The per-project index file, which links to memories rather than being one.
INDEX_FILE = "MEMORY.md"

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_KEY_LINE = re.compile(r"^(\s*)([A-Za-z_][\w-]*):(?:\s+(.*))?$")


def default_source() -> Path:
    """``$CLAUDE_CONFIG_DIR/projects``, else ``~/.claude/projects``."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config_dir) if config_dir else Path.home() / ".claude") / "projects"


@dataclass(frozen=True)
class LocalMemory:
    """One memory file, parsed. ``error`` is set when it cannot be imported."""

    path: Path
    project_dir: str
    name: str = ""
    description: str = ""
    type: str = ""
    body: str = ""
    error: str | None = None

    @property
    def kind(self) -> str | None:
        return KIND_BY_TYPE.get(self.type)

    @property
    def content(self) -> str:
        """The stored text: the one-line description, then the body."""
        return "\n\n".join(part for part in (self.description, self.body) if part)


@dataclass(frozen=True)
class Resolution:
    """Where a Claude Code project directory points: a repo, or why not."""

    repo: str | None
    checkout: Path | None = None
    reason: str | None = None


def encode(path_part: str) -> str:
    """Claude Code's project-directory encoding of a path or path segment."""
    return _NON_ALNUM.sub("-", path_part)


def candidate_paths(project_dir: str, root: Path = Path("/")) -> list[Path]:
    """Existing directories under ``root`` whose absolute path encodes to
    ``project_dir``.

    Walks one level at a time, descending only into entries whose encoding is
    a prefix of what is left, so the cost is proportional to the depth of the
    path rather than the size of the filesystem.
    """
    if not project_dir.startswith("-"):
        return []

    def walk(base: Path, rest: str) -> Iterator[Path]:
        try:
            entries = sorted(e for e in base.iterdir() if e.is_dir())
        except OSError:
            return
        for entry in entries:
            enc = encode(entry.name)
            if rest == enc:
                yield entry
            elif rest.startswith(enc + "-"):
                yield from walk(entry, rest[len(enc) + 1 :])

    return list(walk(root, project_dir[1:]))


def resolve_project(project_dir: str, root: Path = Path("/")) -> Resolution:
    """Map a Claude Code project directory name to a canonical repo URI.

    Only a candidate that is itself a checkout root (has ``.git``) counts, so a
    directory that merely sits inside some other repository (a home directory
    under dotfile management, a folder of checkouts) does not borrow that
    repository's remote.
    """
    candidates = candidate_paths(project_dir, root)
    if not candidates:
        return Resolution(None, reason="no directory on disk matches this name")
    found: dict[str, Path] = {}
    for path in candidates:
        if not (path / ".git").exists():
            continue
        if url := repo_module.git_remote_url(path):
            found.setdefault(normalise(url), path)
    if not found:
        paths = ", ".join(str(p) for p in candidates)
        return Resolution(None, reason=f"not a git checkout with a remote: {paths}")
    if len(found) > 1:
        detail = ", ".join(f"{p} ({r})" for r, p in found.items())
        return Resolution(None, reason=f"ambiguous: {detail}")
    (repo, path), *_ = found.items()
    return Resolution(repo, checkout=path)


def _loose_frontmatter(text: str) -> dict:
    """Read ``key: value`` lines the way Claude Code writes them, not as YAML.

    The agent writes frontmatter by hand and does not quote values, so a
    description such as ``"Ship PR N" means ...`` is invalid YAML (a quoted
    scalar followed by more text) though its meaning is plain. Values are
    taken verbatim to the end of the line; an indented line belongs to the
    last top-level key that had no value of its own (``metadata:``).
    """
    meta: dict = {}
    parent: dict | None = None
    for line in text.splitlines():
        m = _KEY_LINE.match(line)
        if m is None:
            continue
        indent, key, value = m.groups()
        value = (value or "").strip()
        if indent and parent is not None:
            parent[key] = value
        elif not indent:
            meta[key] = value or {}
            parent = meta[key] if isinstance(meta[key], dict) else None
    return meta


def parse(path: Path, project_dir: str) -> LocalMemory:
    """Parse one memory file. Frontmatter problems become ``error``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return LocalMemory(path, project_dir, error=f"unreadable: {exc}")
    match = _FRONTMATTER.match(text)
    if match is None:
        return LocalMemory(path, project_dir, error="no frontmatter")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        meta = _loose_frontmatter(match.group(1))
    if not isinstance(meta, dict):
        return LocalMemory(path, project_dir, error="frontmatter is not a mapping")
    nested = meta.get("metadata")
    # Older files carry `type` at the top level rather than under `metadata`.
    mem_type = (nested.get("type") if isinstance(nested, dict) else None) or meta.get(
        "type"
    )
    name = str(meta.get("name") or "").strip()
    if not name:
        return LocalMemory(path, project_dir, error="frontmatter has no name")
    memory = LocalMemory(
        path,
        project_dir,
        name=name,
        description=str(meta.get("description") or "").strip(),
        type=str(mem_type or "").strip(),
        body=match.group(2).strip(),
    )
    if memory.kind is None:
        return LocalMemory(
            path,
            project_dir,
            name=name,
            type=memory.type,
            error=f"unknown type {memory.type!r}" if memory.type else "no type",
        )
    return memory


def discover(source: Path) -> list[LocalMemory]:
    """Every memory file under ``source``, ordered by project then file."""
    return [
        parse(path, path.parent.parent.name)
        for path in sorted(source.glob("*/memory/*.md"))
        if path.name != INDEX_FILE
    ]
