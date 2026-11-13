"""Claude Code adapter: requires a literal "type" field on MCP entries, and
merges declarative hooks into ~/.claude/settings.json's "hooks" key, deduped
by command per event (mirrors ``witan/setup.py``'s ``_merge_claude_hooks``).

It also reads auto memory (code.claude.com/docs/en/memory, "Auto memory",
fetched 2026-11-13): one directory per project at
``<config dir>/projects/<project>/memory/``, holding a ``MEMORY.md`` index and
one markdown file per memory. Each file's frontmatter carries ``name``,
``description`` and a ``type`` of ``user``, ``feedback``, ``project`` or
``reference`` (under ``metadata:`` in current files, top-level in older ones).
The ``autoMemoryDirectory`` setting, which moves the store elsewhere, is not
followed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from ..memory import MEMORY_TYPES, MemoryEntry, split_frontmatter
from ..models import DeclarativeHook, HookEvent, McpServer, RemoteServer, StdioServer

EVENT_NAMES: dict[HookEvent, str] = {
    HookEvent.PRE_TOOL_USE: "PreToolUse",
    HookEvent.POST_TOOL_USE: "PostToolUse",
    HookEvent.SESSION_START: "SessionStart",
    HookEvent.SESSION_END: "SessionEnd",
    HookEvent.USER_PROMPT_SUBMIT: "UserPromptSubmit",
    HookEvent.STOP: "Stop",
}


def serialize_mcp(server: McpServer) -> dict:
    if isinstance(server, StdioServer):
        data: dict = {"type": "stdio", "command": server.command}
        if server.args:
            data["args"] = server.args
        if server.cwd is not None:
            data["cwd"] = server.cwd
        if server.env:
            data["env"] = server.env
    else:
        assert isinstance(server, RemoteServer)
        # Claude Code's remote MCP entries use "sse"/"http", not a generic
        # "remote" bucket (`claude mcp add --transport sse|http`). Not
        # confirmed against a published schema — ~/.claude.json (where MCP
        # servers actually live) has none — so this follows documented CLI
        # behavior rather than a fetched schema, unlike the OpenCode adapter.
        #
        # Only an explicit transport="sse" emits the deprecated SSE type; any
        # future transport falls through to "http" rather than being silently
        # downgraded onto a transport MCP 2026-07-28 is retiring.
        data = {
            "type": "sse" if server.transport == "sse" else "http",
            "url": server.url,
        }
        if server.headers:
            data["headers"] = server.headers
        # Verbatim passthrough (same approach as the OpenCode adapter): the
        # manifest's canonical oauth shape is {clientId, callbackPort}, which
        # is already Claude Code's own documented `claude mcp add-json` shape
        # (docs.claude.com/en/docs/claude-code/mcp, "pre-configured OAuth
        # credentials" — confirmed 2026-08-31), so no transform is needed here.
        if server.oauth is not None:
            data["oauth"] = server.oauth

    return data


def merge_hooks(settings: dict, hooks: list[DeclarativeHook]) -> None:
    if not isinstance(settings.get("hooks"), dict):
        settings["hooks"] = {}
    hooks_section = settings["hooks"]

    for hook in hooks:
        event_name = EVENT_NAMES[hook.event]
        command_hook: dict = {"type": "command", "command": hook.command}
        # Claude Code's per-command hook object accepts an optional integer
        # "timeout" (seconds); only emit it when set so hooks without one keep
        # their current shape.
        if hook.timeout_seconds is not None:
            command_hook["timeout"] = hook.timeout_seconds
        entry = {"matcher": hook.matcher or "", "hooks": [command_hook]}
        if not isinstance(hooks_section.get(event_name), list):
            hooks_section[event_name] = []
        existing = hooks_section[event_name]
        # Dedup on command. If the command is already registered, refresh its
        # timeout in place (so re-running setup applies a newly-added timeout to
        # an existing install); otherwise append the new entry.
        found = False
        for e in existing:
            if not isinstance(e, dict) or not isinstance(e.get("hooks"), list):
                continue
            for h in e["hooks"]:
                if isinstance(h, dict) and h.get("command") == hook.command:
                    found = True
                    if hook.timeout_seconds is not None:
                        h["timeout"] = hook.timeout_seconds
        if not found:
            existing.append(entry)


def remove_hooks(settings: dict, hooks: list[DeclarativeHook]) -> bool:
    """Inverse of ``merge_hooks``: remove entries matching the given hooks'
    (event, command) identity. Drops a matcher entry entirely once it has no
    remaining hooks, rather than leaving an empty ``"hooks": []`` behind."""
    hooks_section = settings.get("hooks")
    if not isinstance(hooks_section, dict):
        return False
    changed = False
    for hook in hooks:
        event_name = EVENT_NAMES[hook.event]
        entries = hooks_section.get(event_name)
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept.append(entry)
                continue
            remaining = [
                h
                for h in entry["hooks"]
                if not (isinstance(h, dict) and h.get("command") == hook.command)
            ]
            if len(remaining) != len(entry["hooks"]):
                changed = True
            if remaining:
                kept.append({**entry, "hooks": remaining})
        hooks_section[event_name] = kept
    return changed


#: The per-project index file, which links to memories rather than being one.
MEMORY_INDEX = "MEMORY.md"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_DRIVE = re.compile(r"([A-Za-z])--(.*)")


def config_dir() -> Path:
    """``$CLAUDE_CONFIG_DIR``, else ``~/.claude``."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def encode_project(path_part: str) -> str:
    """Claude Code's project-directory encoding of a path or path segment.

    Every character that is not a letter or digit becomes ``-``, so the name
    cannot be decoded: ``-`` may have been ``/``, ``.``, ``_`` or ``-``.
    """
    return _NON_ALNUM.sub("-", path_part)


def project_root(project: str) -> tuple[Path, str] | None:
    """The filesystem root ``project`` starts from, and the rest of it.

    ``/home/me/repo`` encodes as ``-home-me-repo`` and ``C:\\Users\\me\\repo``
    as ``C--Users-me-repo``. A UNC path (``\\\\server\\share``) is not
    recognised: its ``--server-share`` prefix cannot be split into server and
    share.
    """
    if drive := _DRIVE.fullmatch(project):
        return Path(f"{drive.group(1)}:\\"), drive.group(2)
    if project.startswith("-"):
        return Path("/"), project[1:]
    return None


def project_candidates(project: str) -> tuple[Path, ...]:
    """Existing directories whose absolute path encodes to ``project``.

    Walks one level at a time from the root, descending only into entries
    whose encoding is a prefix of what is left, so the cost is proportional to
    the depth of the path rather than the size of the filesystem.
    """
    start = project_root(project)
    if start is None:
        return ()

    def walk(base: Path, rest: str) -> Iterator[Path]:
        try:
            entries = sorted(e for e in base.iterdir() if e.is_dir())
        except OSError:
            return
        for entry in entries:
            enc = encode_project(entry.name)
            if rest == enc:
                yield entry
            elif rest.startswith(enc + "-"):
                yield from walk(entry, rest[len(enc) + 1 :])

    return tuple(walk(*start))


def parse_memory(
    path: Path, project: str, candidates: tuple[Path, ...] = ()
) -> MemoryEntry:
    """One auto-memory file. Problems with it become ``error``."""
    base = MemoryEntry(path, path.stem, project, candidates)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return replace(base, error=f"unreadable: {exc}")
    split = split_frontmatter(text)
    if isinstance(split, str):
        return replace(base, error=split)
    meta, body = split
    nested = meta.get("metadata")
    # Older files carry `type` at the top level rather than under `metadata`.
    mem_type = (nested.get("type") if isinstance(nested, dict) else None) or meta.get(
        "type"
    )
    mem_type = str(mem_type or "").strip()
    name = str(meta.get("name") or "").strip()
    if not name:
        return replace(base, error="frontmatter has no name")
    if mem_type not in MEMORY_TYPES:
        return replace(
            base,
            name=name,
            error=f"unknown type {mem_type!r}" if mem_type else "no type",
        )
    return replace(
        base,
        name=name,
        description=str(meta.get("description") or "").strip(),
        type=mem_type,
        body=body,
    )


def read_memory(root: Path) -> list[MemoryEntry]:
    """Every memory file under ``<config dir>/projects``, by project then file."""
    candidates: dict[str, tuple[Path, ...]] = {}
    entries = []
    for path in sorted(root.glob("*/memory/*.md")):
        if path.name == MEMORY_INDEX:
            continue
        project = path.parent.parent.name
        if project not in candidates:
            candidates[project] = project_candidates(project)
        entries.append(parse_memory(path, project, candidates[project]))
    return entries
