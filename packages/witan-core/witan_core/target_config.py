"""Generic ``[targets.<name>]`` routing shared by witan and witan-code.

A "target" is a named override block in config.toml that routes to different
settings — witan's ``server``/``graph``/``token``, witan-code's ``code_dir``,
or both at once under the same name — based on which repo/org/host/local-path
the current invocation is running against. Each server defines its own typed
override fields (they differ per server); this module owns only the shared
match semantics — parsing the four ``match_*`` lists and picking the winning
target — so both servers apply identical routing rules and a single target
block can drive them together.

Deliberately stdlib-only (no pydantic): the ``witan_core`` root package is a
dependency-free leaf both servers pull regardless of which extras they use,
and this logic doesn't need more than that.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol, TypeVar


def to_list(val: object) -> list[str]:
    """Normalise a TOML value to a list of strings.

    Accepts a list (normal case), a bare string (convenience shorthand for a
    single-element list — env-var ergonomics), or None/missing (returns
    ``[]``). Raises ``ValueError`` for anything else so config errors surface
    early with a clear message.
    """
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        return [str(item) for item in val]
    raise ValueError(f"Expected a list or string, got {type(val).__name__!r}")


def local_project_path() -> Path:
    """The local checkout directory used for ``match_paths`` routing.

    Prefers ``CLAUDE_PROJECT_DIR`` — set by a persistent/global MCP server
    whose own process cwd is not the session's repo, the same escape hatch
    ``witan.repo.detect()``'s ``WITAN_REPO`` and witan-code's
    ``context.py`` use for the analogous problem — over the process cwd.
    """
    if project_dir := os.environ.get("CLAUDE_PROJECT_DIR"):
        return Path(project_dir)
    return Path.cwd()


def parse_target_tables(raw: dict) -> dict[str, dict]:
    """Validate and extract the ``[targets.*]`` tables from a loaded TOML dict.

    Returns ``{name: raw_override_dict}`` — shape-checked only (a table of
    tables); the values are not yet typed, since override fields differ per
    server. Each server builds its own typed target model from these dicts
    (extra/unknown keys are each server's problem to reject or ignore).
    """
    targets = raw.get("targets", {})
    if not isinstance(targets, dict):
        raise ValueError("The 'targets' section in config must be a table.")
    for name, cfg in targets.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"Target {name!r} in config must be a table.")
    return targets


class _MatchCriteria(Protocol):
    """Structural shape a server's own target model must satisfy to be
    routed by :func:`match_target` — just the four match lists, nothing
    server-specific."""

    match_orgs: list[str]
    match_repos: list[str]
    match_hosts: list[str]
    match_paths: list[str]


T = TypeVar("T", bound=_MatchCriteria)


def match_target(
    targets: list[T],
    *,
    repo_uri: str | None = None,
    local_path: Path | None = None,
) -> T | None:
    """Return the first target matching ``repo_uri``/``local_path``.

    Priority (highest first):
    1. ``match_paths`` — local checkout path prefix (e.g. ``~/code/work/``),
       matched against ``local_path`` — most specific: pins a single
       filesystem location regardless of what remote it points at, so it's
       checked even when ``repo_uri`` is unavailable (no remote configured).
    2. ``match_repos`` — suffix match on host+path (e.g.
       "github.com/mitodl/agent-kit" or just "mitodl/agent-kit")
    3. ``match_hosts`` — hostname match (e.g. "github.mit.edu")
    4. ``match_orgs``  — first path segment after host (e.g. "mitodl")

    Tiers 2-4 are skipped when ``repo_uri`` is ``None`` (no remote to match
    against); tier 1 still runs.
    """
    if local_path is not None:
        resolved = local_path.expanduser().resolve()
        for t in targets:
            for pattern in t.match_paths:
                candidate = Path(pattern).expanduser().resolve()
                if resolved == candidate or resolved.is_relative_to(candidate):
                    return t

    if not repo_uri:
        return None

    bare = re.sub(r"^https?://", "", repo_uri).rstrip("/")
    parts = bare.split("/")
    host = parts[0]
    org = parts[1] if len(parts) > 1 else ""

    for t in targets:
        for pattern in t.match_repos:
            p = re.sub(r"^https?://", "", pattern).rstrip("/")
            if bare == p or bare.endswith("/" + p):
                return t

    for t in targets:
        if host and host in t.match_hosts:
            return t

    for t in targets:
        if org and org in t.match_orgs:
            return t

    return None
