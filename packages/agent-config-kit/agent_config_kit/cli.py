"""``ac-kit`` console script — gated behind the ``cli`` extra.

The base ``agent_config_kit`` package stays importable with only ``pydantic``
as a dependency (spec D3); this module is the only place ``cyclopts``/``rich``
are imported, and ``agent_config_kit/__init__.py`` never imports it. Running
the console script without the ``cli`` extra installed fails fast here with a
clear message instead of a bare traceback from deep inside cyclopts/rich.
"""

from __future__ import annotations

import sys

try:
    import cyclopts
    from rich.console import Console
except ImportError as exc:
    sys.stderr.write(
        "ac-kit: the CLI requires the `cli` extra.\n"
        "Install it with: pip install 'agent-config-kit[cli]'\n"
        "(or: uv tool install 'agent-config-kit[cli]')\n"
    )
    raise SystemExit(1) from exc

app = cyclopts.App(
    name="ac-kit",
    help=(
        "Apply and validate manifest-driven MCP server, skill, and hook "
        "registration across coding-agent platforms."
    ),
)
console = Console()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
