"""``witan login`` / ``logout`` / ``whoami`` — remote auth (ADR 0005, path a).

These drive the OIDC device-authorization grant against the deployment named
by ``WITAN_REMOTE_URL`` + ``WITAN_OIDC_ISSUER``. With those unset the CLI runs
in-process and these commands have nothing to talk to, so they say so.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .. import config as cfg_module
from ..identity import derive_actor_id
from ..remote import oidc
from ._common import app, console


def _remote_or_exit() -> cfg_module.RemoteConfig:
    try:
        remote = cfg_module.load_remote_config()
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    if remote is None:
        console.print(
            "[yellow]Remote mode is not configured.[/yellow] Set "
            "[bold]WITAN_REMOTE_URL[/bold] (and [bold]WITAN_OIDC_ISSUER[/bold]) "
            "to point the CLI at a deployed witan service."
        )
        raise SystemExit(1)
    return remote


@app.command
def login() -> None:
    """Authenticate to the deployed witan service via the OIDC device grant.

    Prints a verification URL and a user code; approve it in a browser, and the
    resulting token is cached (mode 0600) and refreshed automatically for
    subsequent ``witan …`` commands.
    """
    remote = _remote_or_exit()

    def _prompt(device: dict) -> None:
        complete = device.get("verification_uri_complete")
        uri = device.get("verification_uri", "")
        code = device.get("user_code", "")
        console.print("\n[bold]Authenticate witan CLI[/bold]")
        if complete:
            console.print(f"  Open: [cyan underline]{complete}[/cyan underline]")
        console.print(
            f"  Or go to [cyan underline]{uri}[/cyan underline] and enter "
            f"code [bold]{code}[/bold]\n  Waiting for approval…"
        )

    try:
        claims = oidc.login(remote, on_prompt=_prompt)
    except oidc.RemoteAuthError as exc:
        console.print(f"[red]Login failed:[/red] {exc}")
        raise SystemExit(1) from None
    who = claims.get("preferred_username") or claims.get("sub", "?")
    console.print(f"[green]Logged in[/green] as [bold]{who}[/bold] → {remote.url}")


@app.command
def logout() -> None:
    """Forget the cached token for the configured deployment."""
    remote = _remote_or_exit()
    if oidc.logout(remote):
        console.print(f"[green]Logged out[/green] of {remote.url}")
    else:
        console.print("[yellow]No cached session to clear.[/yellow]")


@app.command
def whoami() -> None:
    """Show the identity the CLI presents to the deployed witan service."""
    remote = _remote_or_exit()
    try:
        token = oidc.get_valid_token(remote)
    except oidc.NeedsLogin as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise SystemExit(1) from None
    claims = oidc.decode_claims(token)
    sub = claims.get("sub", "")
    if remote.target_name:
        console.print(f"[bold]Target[/bold]    {remote.target_name}")
    console.print(f"[bold]Endpoint[/bold]  {remote.url}")
    console.print(f"[bold]User[/bold]      {claims.get('preferred_username', '?')}")
    if claims.get("email"):
        console.print(f"[bold]Email[/bold]     {claims['email']}")
    console.print(f"[bold]sub[/bold]       {sub}")
    if sub:
        console.print(f"[bold]actor[/bold]     {derive_actor_id(sub)}")
    exp = claims.get("exp")
    if exp:
        when = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        console.print(f"[bold]Expires[/bold]   {when}")
