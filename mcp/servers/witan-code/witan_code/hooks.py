"""SessionStart and PostToolUse hook logic, as plain Python.

Ported from the former codegraph-session-init.sh / codegraph-reindex.sh bash
scripts so hook invocation is a portable CLI command everywhere the
`witan-code` binary installs — Windows included, where bash/setsid don't
exist — matching the bare `witan-code inject-context`/`checkpoint` pattern
this package's other two hooks already use (and witan's own `witan
inject-context`/`session-checkpoint`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import indexer
from . import repo as repo_module
from ._detach import popen_detached
from .context import _lock_path, _project_dir


def session_init() -> None:
    """SessionStart: seed/refresh the whole repo's code graph in the
    background, at most once across overlapping sessions.

    Best-effort and non-blocking: skips non-git directories, never raises,
    and returns immediately — the actual indexing happens in a fully detached
    child process (see :func:`_index_and_unlock`), which is what makes this
    safe to call from a hook that must not block session start.
    """
    project_dir = _project_dir()
    if repo_module.root(project_dir) is None:
        return

    lock = _lock_path(project_dir)
    try:
        lock.mkdir(parents=True)
    except FileExistsError:
        return  # another session is already indexing this repo
    except OSError:
        return

    try:
        popen_detached(
            [
                sys.executable,
                "-m",
                "witan_code",
                "_index-and-unlock",
                str(project_dir),
                str(lock),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        try:
            lock.rmdir()
        except OSError:
            pass


def index_and_unlock(target: Path, lock: Path) -> None:
    """Run by the detached child :func:`session_init` spawns — never called
    directly by a hook. Indexes ``target``, then always releases ``lock``,
    however indexing turns out, so a parse failure can't wedge the lock and
    permanently block future sessions from indexing this repo."""
    try:
        indexer.index_path(target, force=False)
    except Exception:  # noqa: BLE001 — a bad repo must not leave the lock held
        pass
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def reindex_hook(payload: str) -> None:
    """PostToolUse (matcher ``Edit|Write``): incrementally reindex the edited
    file. ``payload`` is the raw hook JSON read from stdin. Foreground and
    fast (a single file), unlike :func:`session_init`'s full-repo background
    index — mirrors the synchronous ``witan-code index <file>`` the old
    reindex hook script ran.

    Best-effort: a missing/malformed payload, an untracked tool, or a parse
    failure all degrade to a silent no-op rather than interrupting the agent.
    """
    try:
        data = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        return
    tool_input = data.get("tool_input") if isinstance(data, dict) else None
    if not isinstance(tool_input, dict):
        return
    raw = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("filename")
    )
    if not isinstance(raw, str) or not raw:
        return

    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        return

    try:
        indexer.index_path(path, force=False)
    except Exception:  # noqa: BLE001 — a parse failure must not fail the hook
        pass
