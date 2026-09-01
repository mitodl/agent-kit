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
from ._common import app, console, esc, print_error
from .code_routing import code_graph_destination
from .selected_target import selected_target


def _remote_or_exit(target: str | None = None) -> cfg_module.RemoteConfig:
    try:
        remote = cfg_module.load_remote_config(target=target)
    except ValueError as exc:
        print_error(exc)
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

    ``--target`` names which ``[targets.<name>]`` block to authenticate
    against — needed for one with no ``match_*`` criteria, since that never
    selects itself. It is an app-level option now (``witan --target ol
    login``, or before the subcommand either way), so it is documented on the
    launcher rather than repeated here.
    """
    remote = _remote_or_exit(selected_target())

    def _prompt(device: dict) -> None:
        complete = device.get("verification_uri_complete")
        uri = device.get("verification_uri", "")
        code = device.get("user_code", "")
        console.print("\n[bold]Authenticate witan CLI[/bold]")
        if complete:
            console.print(f"  Open: [cyan underline]{esc(complete)}[/cyan underline]")
        console.print(
            f"  Or go to [cyan underline]{esc(uri)}[/cyan underline] and enter "
            f"code [bold]{esc(code)}[/bold]\n  Waiting for approval…"
        )

    try:
        claims = oidc.login(remote, on_prompt=_prompt)
    except oidc.RemoteAuthError as exc:
        console.print(f"[red]Login failed:[/red] {esc(exc)}")
        raise SystemExit(1) from None
    who = claims.get("preferred_username") or claims.get("sub", "?")
    console.print(
        f"[green]Logged in[/green] as [bold]{esc(who)}[/bold] → {esc(remote.url)}"
    )


@app.command
def logout() -> None:
    """Forget the cached token for the configured deployment.

    ``--target`` selects which one; see the launcher's help.
    """
    remote = _remote_or_exit(selected_target())
    if oidc.logout(remote):
        console.print(f"[green]Logged out[/green] of {esc(remote.url)}")
    else:
        console.print("[yellow]No cached session to clear.[/yellow]")


def _login_validity(life: oidc.SessionLife) -> str:
    """One line for how long the login lasts, honest about not knowing.

    ``unknown`` is rendered as unknown rather than as anything comforting: it
    means the IdP did not send ``refresh_expires_in``, or the cache entry
    predates this client storing it. Printing "valid" there would be a guess
    with the same shape as a fact.
    """
    # Checked FIRST because it outranks every lifetime below it: with no refresh
    # token there is nothing to renew, so the login ends when the access token
    # does no matter what any expiry says.
    if not life.renewable:
        return "ends when the token above expires — no refresh token was issued"
    if life.refresh_state == "never":
        return "does not expire (offline token)"
    if life.refresh_state == "unknown":
        return "unknown — the IdP did not report a refresh lifetime"
    remaining = life.refresh_expires_at - datetime.now(tz=timezone.utc).timestamp()
    # Whole seconds, matching the `exp` claim on the line above — the refresh
    # expiry is computed locally and would otherwise print six decimal places
    # of spurious precision next to a timestamp that has none.
    when = datetime.fromtimestamp(
        int(life.refresh_expires_at), tz=timezone.utc
    ).isoformat()
    if remaining <= 0:
        return f"EXPIRED at {when} — run `witan login`"
    hours, minutes = divmod(int(remaining) // 60, 60)
    span = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    return f"{when} ({span} left)"


@app.command
def whoami() -> None:
    """Show the identity the CLI presents to the deployed witan service.

    ``--target`` selects which one; see the launcher's help.
    """
    remote = _remote_or_exit(selected_target())
    try:
        token = oidc.get_valid_token(remote)
    except oidc.NeedsLogin as exc:
        console.print(f"[yellow]{esc(exc)}[/yellow]")
        raise SystemExit(1) from None
    except oidc.RemoteAuthError as exc:
        # Caught separately from NeedsLogin above so a token endpoint that is
        # merely unreachable does not read as "log in again" — the whole point
        # of classifying the two.
        print_error(exc)
        raise SystemExit(1) from None
    claims = oidc.decode_claims(token)
    sub = claims.get("sub", "")
    if remote.target_name:
        console.print(f"[bold]Target[/bold]    {remote.target_name}")
    console.print(f"[bold]Endpoint[/bold]  {esc(remote.url)}")
    # "What am I pointed at?" is the question this command exists to answer,
    # and until this it answered only the identity half — while the OTHER half
    # is routed by a separate setting that can, and on production did, disagree
    # with this one for months without anything saying so.
    # An unreadable code config is REPORTED here, not swallowed. This is the
    # only place whoami loads it, so returning None on a bad `code_transport`
    # would drop the line entirely and leave the misconfiguration looking like
    # "witan-code isn't installed". Printed rather than raised: the identity
    # half below is still correct and still worth showing.
    try:
        code_dest = code_graph_destination(remote.target_name)
    except ValueError as exc:
        console.print(f"[bold]Code[/bold]      [red]unreadable — {esc(str(exc))}[/red]")
    else:
        if code_dest:
            console.print(f"[bold]Code[/bold]      {esc(code_dest)}")
    console.print(
        f"[bold]User[/bold]      {esc(claims.get('preferred_username', '?'))}"
    )
    if claims.get("email"):
        console.print(f"[bold]Email[/bold]     {esc(claims['email'])}")
    console.print(f"[bold]sub[/bold]       {sub}")
    if sub:
        console.print(f"[bold]actor[/bold]     {derive_actor_id(sub)}")
    # TWO clocks, and the second is the one that answers "will I have to log in
    # again?". Reporting only the access token's expiry showed a number minutes
    # away and invited the reader to conclude their login was about to lapse,
    # when a refresh renews it silently and the session may have days left.
    life = oidc.session_life(remote)
    exp = claims.get("exp")
    if exp:
        when = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        # "renews automatically" is conditional on there being something to
        # renew with. A token response may carry no refresh_token at all — the
        # cache accepts that — and promising renewal there is a claim the next
        # call disproves with a NeedsLogin.
        renews = " (renews automatically)" if life.renewable else ""
        console.print(f"[bold]Token[/bold]     {when}{renews}")
    console.print(f"[bold]Login[/bold]     {_login_validity(life)}")
