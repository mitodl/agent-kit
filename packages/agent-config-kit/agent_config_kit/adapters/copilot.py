"""GitHub Copilot / VS Code adapter: requires a literal "type" field on
entries under <vscode-user-dir>/mcp.json's "servers" key. "type" is
"stdio"/"sse"/"http" — never a generic "remote" bucket — and remote-only
fields (headers/oauth) aren't part of the hand-authored wire model this was
verified against (see adapters/_wire/copilot_mcp.py; no upstream schema is
publicly fetchable for VS Code's mcp.json, per the design spec's open
questions), so they're intentionally dropped rather than guessed at.

Copilot Chat's memory tool (microsoft/vscode ``extensions/copilot``,
``tools/node/memoryTool.tsx`` and ``memoryContextPrompt.tsx``, read at commit
577d0d5, 2026-11-13) keeps three scopes of free-form markdown files under
``memory-tool/memories`` in the extension's storage. Repository memory
(``/memories/repo/``, "codebase conventions, build commands, project
structure facts") is in workspace storage,
``<user dir>/workspaceStorage/<id>/GitHub.copilot-chat/``, and VS Code writes
the workspace that ``<id>`` belongs to into ``<id>/workspace.json`` as
``{"folder": "<uri>"}`` (``src/vs/platform/storage/electron-main/
storageMain.ts``). That scope is what ``read_memory`` returns. User memory, in
global storage, applies to every workspace, and session memory is one
conversation's scratch notes, so neither is read.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from ..memory import MemoryEntry
from ..models import McpServer, RemoteServer, StdioServer


def serialize_mcp(server: McpServer) -> dict:
    if isinstance(server, StdioServer):
        data: dict = {"type": "stdio", "command": server.command}
        if server.args:
            data["args"] = server.args
        if server.env:
            data["env"] = server.env
        return data

    assert isinstance(server, RemoteServer)
    # Only an explicit transport="sse" emits the type MCP 2026-07-28 deprecates;
    # anything else maps to "http" rather than falling back onto it.
    return {
        "type": "sse" if server.transport == "sse" else "http",
        "url": server.url,
    }


#: Repository memory under one workspace's storage directory. Workspace
#: storage keeps the extension id's case (``extHostStoragePaths.ts``).
REPO_MEMORY_DIR = Path("GitHub.copilot-chat") / "memory-tool" / "memories" / "repo"

_HEADING = re.compile(r"^#+\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _workspace(storage: Path) -> tuple[str, tuple[Path, ...]]:
    """The workspace a storage directory belongs to, and its folder on disk.

    Only a single-folder workspace on this machine (a ``file:`` URI) has a
    folder; a multi-root ``.code-workspace`` or a remote (``vscode-remote:``)
    folder is labelled by its URI with no candidates.
    """
    try:
        meta = json.loads((storage / "workspace.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return storage.name, ()
    uri = meta.get("folder") if isinstance(meta, dict) else None
    if not isinstance(uri, str):
        label = meta.get("workspace") if isinstance(meta, dict) else None
        return (label if isinstance(label, str) else storage.name), ()
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri, ()
    folder = Path(url2pathname(unquote(parsed.path)))
    return str(folder), ((folder,) if folder.is_dir() else ())


def read_memory(root: Path) -> list[MemoryEntry]:
    """Every repository-memory file under ``<user dir>/workspaceStorage``."""
    entries = []
    for storage in sorted(p for p in root.iterdir() if p.is_dir()):
        folder = storage / REPO_MEMORY_DIR
        files = sorted(folder.rglob("*.md")) if folder.is_dir() else []
        if not files:
            continue
        project, candidates = _workspace(storage)
        for path in files:
            key = path.relative_to(folder).with_suffix("").as_posix()
            base = MemoryEntry(path, key, project, candidates)
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as exc:
                entries.append(replace(base, error=f"unreadable: {exc}"))
                continue
            if not text:
                entries.append(replace(base, error="empty"))
                continue
            # A topic file has no frontmatter; its first heading, if any, names it.
            heading = _HEADING.search(text)
            entries.append(
                replace(
                    base,
                    name=heading.group(1) if heading else path.stem,
                    type="project",
                    body=text,
                )
            )
    return entries
