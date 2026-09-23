"""Pi adapter: no "type" field on MCP entries — stdio and remote servers share
one shape distinguished by presence of "command" vs "url" (see
adapters/_wire/pi_mcp.py).

Skills install only to Pi's own skills dir (~/.pi/agent/skills/), not also to
the shared cross-agent pool (~/.agents/skills/): Pi already natively unions
both directories when discovering skills (its own docs/skills.md "Locations"
section), so writing the same skill into both is pure duplication — Pi finds
the name twice and logs a spurious "skill collision" warning on every
startup. A single dest dir is correct and sufficient.

Pi core has no MCP support of its own: the mcp.json files this adapter
writes are read only by the third-party ``pi-mcp-adapter`` Pi package.
``mcp_adapter_prerequisite`` is the read-only preflight ``plan.apply`` runs
to say whether that package looks installed (see its docstring).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..jsonio import load_json_object
from ..models import McpServer, RemoteServer, Scope, StdioServer

MCP_ADAPTER_PACKAGE = "pi-mcp-adapter"
MCP_ADAPTER_INSTALL = "pi install npm:pi-mcp-adapter"

# Matches every way Pi's docs/packages.md lets a source name the package:
# "npm:pi-mcp-adapter", "npm:pi-mcp-adapter@2.37.0",
# "git:github.com/<owner>/pi-mcp-adapter@v2", "https://.../pi-mcp-adapter.git",
# or a local path ending in the package directory — but not a differently
# named package that merely starts with the same text.
_ADAPTER_SOURCE = re.compile(r"(?:^|[/:\\])pi-mcp-adapter(?:$|[@/#\\]|\.git\b)")


def _names_adapter(source: object) -> bool:
    return isinstance(source, str) and bool(_ADAPTER_SOURCE.search(source.strip()))


def _settings_declare_adapter(path: Path) -> bool:
    """Whether the Pi settings file at ``path`` loads pi-mcp-adapter.

    Pi declares packages in settings.json's ``packages`` array, each either a
    source string or ``{"source": ..., "extensions": [...], ...}``
    (docs/packages.md); an object whose ``extensions`` filter is ``[]`` loads
    none of the package's extensions, so it does not count. A bare
    extension path in the ``extensions`` array also counts, minus ``!``/``-``
    exclusions (docs/settings.md "Resources").

    A file that cannot be read (permissions, not UTF-8) reads as not
    declaring the adapter: this check is informational and runs after
    mcp.json is written, so it must never abort the rest of ``apply``."""
    try:
        cfg = load_json_object(path) if path.is_file() else None
    except (OSError, UnicodeDecodeError):
        return False
    if not cfg:
        return False
    packages = cfg.get("packages")
    for entry in packages if isinstance(packages, list) else []:
        if isinstance(entry, dict):
            if entry.get("extensions") == []:
                continue
            entry = entry.get("source")
        if _names_adapter(entry):
            return True
    extensions = cfg.get("extensions")
    for entry in extensions if isinstance(extensions, list) else []:
        if (
            isinstance(entry, str)
            and not entry.startswith(("!", "-"))
            and _names_adapter(entry.lstrip("+"))
        ):
            return True
    return False


def mcp_adapter_prerequisite(scope: Scope) -> tuple[bool, str]:
    """Read-only check for pi-mcp-adapter: never runs ``pi``/``npm`` or
    touches the network, only reads the settings files ``pi install`` writes.

    ``pi install`` records a package in ``~/.pi/agent/settings.json`` (or,
    with ``-l``, the project's ``.pi/settings.json``). A global MCP entry is
    only honored everywhere when the adapter is installed globally, so global
    scope checks just the global settings file; project scope also accepts a
    project-local declaration. Best-effort: a package disabled through
    ``pi config``, or a project package Pi has not yet been granted trust to
    load, still reads as present."""
    global_settings = Path.home() / ".pi" / "agent" / "settings.json"
    candidates = [global_settings]
    if scope == Scope.PROJECT:
        candidates.append(Path(".pi") / "settings.json")
    for path in candidates:
        if _settings_declare_adapter(path):
            return True, f"{MCP_ADAPTER_PACKAGE} is declared in {path}"
    checked = " or ".join(str(p) for p in candidates)
    local_hint = (
        f" (or `{MCP_ADAPTER_INSTALL} -l` for this project only)"
        if scope == Scope.PROJECT
        else ""
    )
    return False, (
        f"{MCP_ADAPTER_PACKAGE} was not found in {checked}; Pi will ignore "
        f"these MCP servers until it is installed. Run `{MCP_ADAPTER_INSTALL}`"
        f"{local_hint}, restart Pi, then confirm with `pi list` (and `/mcp` "
        "inside Pi)."
    )


def serialize_mcp(server: McpServer) -> dict:
    if isinstance(server, StdioServer):
        data: dict = {"command": server.command}
        if server.args:
            data["args"] = server.args
        # cwd and headers are ServerEntry fields in pi-mcp-adapter's own
        # types.ts (verified against the installed adapter, v2.37.0), so they
        # pass through verbatim, the same way the Claude adapter emits them.
        # The adapter expands ${VAR}/~ in cwd and ${VAR}/!command in header
        # values itself, so no rewriting happens here.
        if server.cwd is not None:
            data["cwd"] = server.cwd
        if server.env:
            data["env"] = server.env
        return data

    assert isinstance(server, RemoteServer)
    data = {"url": server.url}
    if server.headers:
        data["headers"] = server.headers
    # The manifest's canonical oauth shape is {clientId, callbackPort},
    # matching Claude Code's own documented shape (see claude.py) — but Pi's
    # real shape differs in two ways: a top-level "auth": "oauth"
    # discriminator, and "callbackPort" (an int) becomes "redirectUri" (a
    # full localhost callback URL); Pi has no "callbackPort" concept of its
    # own. Every other field (clientSecret, scope, redirectUri,
    # authServerMetadataUrl — see OAuthConfig in _wire/pi_mcp.py) is a
    # shared, identically-named field across both shapes, so those pass
    # through untouched rather than being silently dropped. A manifest that
    # sets redirectUri explicitly wins over one derived from callbackPort.
    # Verified against pi.dev/packages/pi-mcp-adapter (live docs fetched
    # 2026-08-31) — not against ``_wire/pi_mcp.py``, which predates Pi's
    # OAuth support and was hand-authored (no published schema exists for
    # Pi to codegen from, per spec D6), so it was hand-updated alongside
    # this adapter rather than regenerated. Re-verify both together if Pi's
    # OAuth config shape changes.
    if server.oauth is not None:
        data["auth"] = "oauth"
        oauth = dict(server.oauth)
        callback_port = oauth.pop("callbackPort", None)
        if callback_port is not None and "redirectUri" not in oauth:
            oauth["redirectUri"] = f"http://localhost:{callback_port}/callback"
        data["oauth"] = oauth
    return data
