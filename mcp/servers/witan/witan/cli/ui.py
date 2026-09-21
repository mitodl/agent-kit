"""Open the witan UI: ``witan ui``."""

from __future__ import annotations

import socket
import threading
import webbrowser
from typing import Annotated

import cyclopts

from .. import config as cfg_module
from .. import ui_routes
from ._common import app, console, print_error
from .selected_target import selected_target


def _free_port() -> int:
    """A port the OS says is free.

    Bound to loopback for the probe, not just to pick a number: binding to
    0.0.0.0 here would briefly open the port to the network, and on a
    locked-down machine that is the part that gets refused.

    Inherently racy (the socket closes before uvicorn binds), which is why
    ``--port`` exists. A retry loop would not fix it either, since the race is
    between this process and any other on the machine.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@app.command
def ui(
    *,
    port: Annotated[int | None, cyclopts.Parameter(env_var="WITAN_UI_PORT")] = None,
    browser: bool = True,
) -> None:
    """Open the witan UI in a browser.

    Against the local store this starts a witan server on 127.0.0.1 serving
    both the page and ``/mcp``, and opens it. The page is an MCP client
    (ADR 0011), so serving both from one origin is what makes its tool calls
    same-origin: no CORS, no preflight per call, and fastmcp's Host/Origin
    guard admits the page under its same-origin rule without an allowlist.

    ★ AGAINST A REMOTE TARGET THIS OPENS A BROWSER AND EXITS, rather than
    starting anything. ``witan serve`` refuses to re-serve a remote target over
    HTTP for a reason that applies here verbatim: every forwarded call would
    carry the cached OIDC token of whoever started the process, and this server
    has no inbound authentication of its own, so binding it to a socket turns
    it into a credential-sharing proxy. The deployment serves its own copy of
    this page and does its own login.

    Parameters
    ----------
    port: Port to bind. Defaults to one the OS says is free.
        Env: ``WITAN_UI_PORT``.
    browser: Open a browser. ``--no-browser`` just prints the URL.
    """
    from witan_core.observability import configure_observability

    configure_observability(instrument=False)

    try:
        remote = cfg_module.load_remote_config(target=selected_target())
    except ValueError as exc:
        print_error(exc, stderr=True)
        raise SystemExit(1) from None

    if remote is not None:
        _open_remote(remote, browser=browser)
        return

    if not ui_routes.bundle_exists():
        # Loudly, and naming the command. A server that starts and then 404s
        # every page read looks like a broken install rather than a missing
        # build step.
        print_error(
            "No UI bundle was built, so there is nothing to serve.\n"
            "Build it with:  cd mcp/servers/witan/ui && npm ci && npm run build",
            stderr=True,
        )
        raise SystemExit(1)

    _serve_local(port or _free_port(), browser=browser)


def _open_remote(remote, *, browser: bool) -> None:
    """Point the browser at the deployment's own copy of the page.

    `rstrip` BEFORE `removesuffix`, because `RemoteConfig.url` is stored
    verbatim from config with no normalization: a perfectly ordinary
    `https://witan.example.org/mcp/` would otherwise keep its `/mcp` and open
    `/mcp/ui/`.
    """
    url = remote.url.rstrip("/").removesuffix("/mcp").rstrip("/") + "/ui/"
    console.print(f"Opening the deployed witan UI at [bold]{url}[/bold]")
    console.print("[dim]It does its own login; nothing is served from here.[/dim]")
    if browser:
        _open(url)


def _open(url: str) -> None:
    """Open a URL, treating a failure to do so as cosmetic.

    `webbrowser.open` returns False for some failures but RAISES for others:
    a `BROWSER` naming a command that does not exist reaches `Popen` and comes
    back as `OSError`. The URL has already been printed by the time this runs,
    so a browser that will not start is a minor inconvenience, not a reason to
    end in a traceback.
    """
    try:
        webbrowser.open(url)
    except OSError as exc:
        console.print(f"[dim]Could not open a browser ({exc}); the URL is above.[/dim]")


def _serve_local(port: int, *, browser: bool) -> None:
    """Run the server on loopback and open the page."""
    from ..server import mcp as witan_mcp

    # The same mount `witan serve` does. Without it `witan ui` would serve a
    # strictly smaller tool surface than the deployment, so the page would see
    # the code graph on one and not the other for no reason a reader could
    # find.
    try:
        from witan_code.server import mcp as code_mcp

        witan_mcp.mount(code_mcp)
    except ImportError:
        pass

    url = f"http://127.0.0.1:{port}/ui/"
    console.print(f"witan UI on [bold]{url}[/bold]   [dim](ctrl-c to stop)[/dim]")

    if browser:
        # After a delay, on a daemon thread: uvicorn's run() never returns, so
        # opening first would race the bind and opening after is unreachable.
        # A browser that loses the race shows a connection error on a URL the
        # user can simply reload, which is why this is not worth polling for.
        threading.Timer(0.7, webbrowser.open, args=(url,)).start()

    witan_mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=port,
        path="/mcp",
        # ON THIS BIND THE GUARD IS THE ONLY THING BETWEEN THE GRAPH AND ANY
        # PAGE THE USER HAS OPEN: the endpoint is unauthenticated, so without
        # it a foreign page can POST tool calls to 127.0.0.1 and read
        # everything. fastmcp defaults it off (settings.py:280), so passing it
        # explicitly here is what turns it on for `witan ui`.
        host_origin_protection="auto",
    )
