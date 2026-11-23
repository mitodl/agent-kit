"""The witan language server host (ADR 0012, "2. The language server").

This package and :mod:`.plugin`, :mod:`.folders` and :mod:`.host` are stdlib
only, so a server's plugin module can import them without the ``lsp`` extra.
:mod:`.server` (the stdio entrypoint) and :mod:`.testing` need pygls.
"""

from __future__ import annotations

import sys

from .plugin import (
    BOUND_TOOLS,
    ENTRY_POINT_GROUP,
    Capability,
    CommandContext,
    Context,
    Folder,
    Lens,
    Location,
    Note,
    Plugin,
    Route,
    escape_markdown,
)

__all__ = [
    "BOUND_TOOLS",
    "ENTRY_POINT_GROUP",
    "Capability",
    "CommandContext",
    "Context",
    "Folder",
    "Lens",
    "Location",
    "Note",
    "Plugin",
    "Route",
    "escape_markdown",
    "run_language_server",
]


def run_language_server(target: str | None, *, package: str) -> None:
    """The body of ``witan lsp`` and ``witan-code lsp``.

    Exits 1 with a message on stderr when ``package``'s ``lsp`` extra is not
    installed or the target is undefined, which happens before the protocol
    starts, so an editor shows it as the server's startup error.
    """
    try:
        from .server import serve
    except ImportError as exc:
        print(
            f"witan: the language server needs the lsp extra ({exc}). Install "
            f"it with: uv tool install '{package}[lsp]'",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    try:
        serve(target)
    except ValueError as exc:
        print(f"witan: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
