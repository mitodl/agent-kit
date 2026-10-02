"""Pi adapter: no "type" field on MCP entries — stdio and remote servers share
one shape distinguished by presence of "command" vs "url" (see
adapters/_wire/pi_mcp.py).

Skills install only to Pi's own skills dir (~/.pi/agent/skills/), not also to
the shared cross-agent pool (~/.agents/skills/): Pi already natively unions
both directories when discovering skills (its own docs/skills.md "Locations"
section), so writing the same skill into both is pure duplication — Pi finds
the name twice and logs a spurious "skill collision" warning on every
startup. A single dest dir is correct and sufficient.

Pi 0.99 and later reads the mcp.json files this adapter writes with its
built-in MCP extension (``builtin:mcp``). The third-party ``pi-mcp-adapter``
package, when installed, replaces that extension and reads the same files
in the same format. ``mcp_prerequisite`` is the read-only preflight
``plan.apply`` runs to say whether either one will load them (see its
docstring).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..jsonio import parse_json_object
from ..models import McpServer, RemoteServer, Scope, StdioServer

MCP_ADAPTER_PACKAGE = "pi-mcp-adapter"
MCP_ADAPTER_INSTALL = "pi install npm:pi-mcp-adapter"
BUILTIN_MCP = "builtin:mcp"

# Matches every way Pi's docs/packages.md lets a source name the package:
# "npm:pi-mcp-adapter", "npm:pi-mcp-adapter@2.37.0",
# "git:github.com/<owner>/pi-mcp-adapter@v2", "https://.../pi-mcp-adapter.git",
# or a local path ending in the package directory — but not a differently
# named package that merely starts with the same text.
_ADAPTER_SOURCE = re.compile(r"(?:^|[/:\\])pi-mcp-adapter(?:$|[@/#\\]|\.git\b)")


def _names_adapter(source: object) -> bool:
    return isinstance(source, str) and bool(_ADAPTER_SOURCE.search(source.strip()))


def _load_settings(path: Path) -> dict | None:
    """The Pi settings file at ``path`` as a JSON object (``{}`` when it is
    missing or not a JSON object), or ``None`` when it exists but cannot be
    opened.

    The file is decoded the way Pi's settings-manager reads it
    (``readFileSync(path, "utf-8")``, which replaces invalid bytes, then a
    stripped BOM), so a stray non-UTF-8 byte elsewhere in the file does not
    hide a declaration Pi itself honors. A file that cannot be opened at all
    (e.g. permissions) returns ``None`` rather than raising: this check is
    informational and runs after mcp.json is written, so it must never abort
    the rest of ``apply``."""
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_json_object(text.removeprefix("\ufeff")) or {}


def _declares_adapter(cfg: dict) -> bool:
    """Whether a Pi settings object loads pi-mcp-adapter.

    Pi declares packages in settings.json's ``packages`` array, each either a
    source string or ``{"source": ..., "extensions": [...], ...}``
    (docs/packages.md); an object whose ``extensions`` filter is ``[]`` loads
    none of the package's extensions, so it does not count. A bare
    extension path in the ``extensions`` array also counts, minus ``!``/``-``
    exclusions (docs/settings.md "Resources")."""
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


def _builtin_mcp_setting(cfg: dict) -> bool | None:
    """Whether a Pi settings object turns the built-in MCP extension on
    (``+builtin:mcp``) or off (``-builtin:mcp``, what ``pi config`` writes,
    and what pi-mcp-adapter writes when it is installed), or ``None`` when
    it says nothing. The last matching entry wins. A ``!`` prefix counts as
    off, the same way pi-mcp-adapter reads it."""
    setting = None
    extensions = cfg.get("extensions")
    for entry in extensions if isinstance(extensions, list) else []:
        if isinstance(entry, str) and entry.strip().lstrip("+!-") == BUILTIN_MCP:
            setting = not entry.strip().startswith(("!", "-"))
    return setting


def mcp_prerequisite(scope: Scope) -> tuple[bool, str]:
    """Read-only check that something in Pi will load the mcp.json being
    written: never runs ``pi``/``npm`` or touches the network, only reads
    the settings files ``pi install`` and ``pi config`` write.

    pi-mcp-adapter counts when it is declared: ``pi install`` records it in
    ``~/.pi/agent/settings.json`` (or, with ``-l``, the project's
    ``.pi/settings.json``). A global MCP entry is only honored everywhere
    when the adapter is installed globally, so global scope checks just the
    global settings file; project scope also accepts a project-local
    declaration.

    Otherwise Pi's built-in MCP counts unless ``-builtin:mcp`` turns it off.
    Built-in extensions load by default, and a project's ``+``/``-`` entry
    overrides the user's (docs/settings.md "Resources"), so global scope
    reads the global setting and project scope lets the project's win.
    Removing pi-mcp-adapter leaves the ``-builtin:mcp`` it added behind, so
    that case reports as missing.

    Best-effort: a package disabled through ``pi config``, or a project
    package Pi has not yet been granted trust to load, still reads as
    present, and the Pi version is not checked (the built-in needs 0.99 or
    later)."""
    global_settings = Path.home() / ".pi" / "agent" / "settings.json"
    candidates = [global_settings]
    if scope == Scope.PROJECT:
        candidates.append(Path(".pi") / "settings.json")
    loaded = [(path, _load_settings(path)) for path in candidates]
    for path, cfg in loaded:
        if cfg is not None and _declares_adapter(cfg):
            return True, f"{MCP_ADAPTER_PACKAGE} is declared in {path}"
    unreadable = [path for path, cfg in loaded if cfg is None]
    if unreadable:
        names = " or ".join(str(p) for p in unreadable)
        return False, (
            f"could not read {names}, so whether Pi's built-in MCP is turned "
            f"on or {MCP_ADAPTER_PACKAGE} is installed is unknown; Pi ignores "
            "these MCP servers without one of them. Fix the file's "
            "permissions, then confirm with `pi mcp list`."
        )
    builtin_on, decided_by = True, None
    for path, cfg in loaded:
        setting = _builtin_mcp_setting(cfg or {})
        if setting is not None:
            builtin_on, decided_by = setting, path
    if builtin_on:
        where = f"turned on in {decided_by}" if decided_by else "not turned off"
        return True, (
            f"Pi's built-in MCP is {where} (it needs Pi 0.99 or later; "
            "confirm with `pi mcp list`)"
        )
    checked = " or ".join(str(p) for p in candidates)
    local_hint = (
        f" (or `{MCP_ADAPTER_INSTALL} -l` for this project only)"
        if scope == Scope.PROJECT
        else ""
    )
    return False, (
        f"Pi's built-in MCP is turned off in {decided_by} (`-{BUILTIN_MCP}`) "
        f"and {MCP_ADAPTER_PACKAGE} was not found in {checked}, so Pi will "
        "ignore these MCP servers. Turn the built-in back on under Built-in "
        f"in `pi config` (or remove `-{BUILTIN_MCP}` from that file), or run "
        f"`{MCP_ADAPTER_INSTALL}`{local_hint}; then restart Pi and confirm "
        "with `pi mcp list`."
    )


def serialize_mcp(server: McpServer) -> dict:
    if isinstance(server, StdioServer):
        data: dict = {"command": server.command}
        if server.args:
            data["args"] = server.args
        # cwd and headers pass through verbatim, the same way the Claude
        # adapter emits them: Pi expands a leading ~/ in command, args and
        # cwd, and ${VAR}/!command in env and header values, itself
        # (docs/mcp.md "Configure servers").
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
    # matching Claude Code's own documented shape (see claude.py). Pi's
    # (docs/mcp.md "Authenticate with OAuth", and the validator in
    # dist/core/mcp-servers.js, Pi 1.0.0) differs in one way that matters:
    # Pi turns callbackPort into http://127.0.0.1:<port>/callback, while
    # Claude Code and this adapter's earlier "redirectUri" output used
    # http://localhost:<port>/callback. A pre-registered client needs the
    # exact URI, so the port becomes a callbackUrl that keeps localhost. A
    # manifest's own callbackUrl wins over that, and a redirectUri (the
    # field pi-mcp-adapter 2.x read) is carried over as the callbackUrl.
    # Every other field (clientSecret, scope, clientName,
    # authServerMetadataUrl) is named the same in both shapes and passes
    # through untouched.
    #
    # There is no "auth": "oauth" discriminator any more: Pi signs in with
    # OAuth whenever an HTTP server sends no Authorization header, and
    # reserves "auth" for {"provider": ...}. Both Pi and pi-mcp-adapter 5.x
    # (which reads these files on Pi 0.99 and later) skip an entry whose
    # "auth" is a string.
    if server.oauth is not None:
        oauth = dict(server.oauth)
        redirect_uri = oauth.pop("redirectUri", None)
        if "callbackUrl" in oauth:
            pass  # explicit; Pi reads it with any callbackPort beside it
        elif redirect_uri is not None:
            oauth.pop("callbackPort", None)
            oauth["callbackUrl"] = redirect_uri
        elif "callbackPort" in oauth:
            port = oauth.pop("callbackPort")
            oauth["callbackUrl"] = f"http://localhost:{port}/callback"
        data["oauth"] = oauth
    return data
