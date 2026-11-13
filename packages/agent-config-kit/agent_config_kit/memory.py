"""Agent-written persistent memory: where a platform keeps it and how to read it.

Read-only. A platform's ``AgentPlatform.memory`` names the directory its agent
writes memory into and a reader that turns that directory into entries, each
tagged with the project it belongs to. Nothing here writes into a platform's
store, and nothing here decides what an entry means to a consumer (witan maps
``MemoryEntry.type`` to its own memory kinds).

A platform with no file-based agent memory has ``memory=None``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

import yaml

#: What an entry is about, in Claude Code's auto-memory vocabulary, the one
#: platform that types its memories. ``user`` is about a person (role,
#: preferences); ``feedback`` is a correction or a confirmed approach;
#: ``project`` is a fact about the work; ``reference`` points somewhere
#: outside the project. Other platforms' readers pick the closest one.
MemoryType = Literal["user", "feedback", "project", "reference"]
MEMORY_TYPES: tuple[str, ...] = get_args(MemoryType)


@dataclass(frozen=True)
class MemoryEntry:
    """One memory as the platform stored it. ``error`` is set when it can't be read.

    ``project`` is the store's own label for the project the entry belongs to
    (a Claude Code project directory name, a Codex session's cwd, a VS Code
    workspace folder URI). ``candidates`` are the existing directories that
    label can stand for: one for a store that records the path, any number for
    one whose label is a lossy encoding of it, none when nothing on disk
    matches. Which of them is a repository is the consumer's call.

    ``key`` tells entries of one project in one store apart (a file stem, a
    rollout name).
    """

    path: Path
    key: str
    project: str
    candidates: tuple[Path, ...] = ()
    name: str = ""
    description: str = ""
    type: MemoryType | None = None
    body: str = ""
    error: str | None = None

    @property
    def content(self) -> str:
        """The text worth keeping: the one-line description, then the body."""
        return "\n\n".join(part for part in (self.description, self.body) if part)


@dataclass(frozen=True)
class MemoryStore:
    """Where a platform's agent keeps memory, and how to read it back.

    ``read`` gets ``root`` and returns every entry under it; ``entries`` calls
    it only when ``root`` exists.
    """

    root: Path
    read: Callable[[Path], list[MemoryEntry]]

    def entries(self) -> list[MemoryEntry]:
        return self.read(self.root) if self.root.exists() else []


_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
_KEY_LINE = re.compile(r"^(\s*)([A-Za-z_][\w-]*):(?:\s+(.*))?$")
_BLOCK_SCALAR = re.compile(r"^\s*[A-Za-z_][\w-]*:\s+[|>][-+0-9]*\s*$", re.MULTILINE)


def _unquote(value: str) -> str:
    """``value`` without its quotes, when it is one quoted YAML string."""
    if len(value) < 2 or value[0] not in "\"'" or value[-1] != value[0]:
        return value
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        return value
    return parsed if isinstance(parsed, str) else value


def _loose_frontmatter(text: str) -> dict:
    """Read ``key: value`` lines the way an agent writes them, not as YAML.

    Agents write frontmatter by hand and mostly do not quote values. As YAML,
    a description such as ``"Ship PR N" means ...`` does not parse (a quoted
    scalar followed by more text), and ``Merge PR #614 first`` parses but
    loses everything from `` #`` on as a comment. So a value is taken verbatim
    to the end of the line, and unquoted only when the quotes wrap all of it.
    An indented line belongs to the last top-level key that had no value of
    its own (``metadata:``).
    """
    meta: dict = {}
    parent: dict | None = None
    for line in text.splitlines():
        m = _KEY_LINE.match(line)
        if m is None:
            continue
        indent, key, value = m.groups()
        value = _unquote((value or "").strip())
        if indent and parent is not None:
            parent[key] = value
        elif not indent:
            meta[key] = value or {}
            parent = meta[key] if isinstance(meta[key], dict) else None
    return meta


def split_frontmatter(text: str) -> tuple[dict, str] | str:
    """``(frontmatter, body)`` of agent-written markdown, or why it has none.

    A file with a ``>``/``|`` block-scalar value was written as YAML (its
    continuation lines would be dropped by the line reader) and is read as
    YAML; anything else is read line by line (``_loose_frontmatter``).
    """
    match = _FRONTMATTER.match(text)
    if match is None:
        return "no frontmatter"
    frontmatter, body = match.groups()
    if not _BLOCK_SCALAR.search(frontmatter):
        return _loose_frontmatter(frontmatter), body.strip()
    try:
        meta = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        return "frontmatter is not valid YAML"
    if not isinstance(meta, dict):
        return "frontmatter is not a mapping"
    return meta, body.strip()
