"""``witan target add|list|remove`` — manage ``[targets.<name>]`` config blocks.

Joining a deployed witan used to mean hand-writing TOML: two URLs you had to
get exactly right, in a file you had to know the path of. A typo in the issuer
did not surface as a config error — it surfaced later, as an auth failure
during ``witan login``.

So ``target add`` validates the issuer *at registration time* by fetching its
OIDC discovery document (the same :func:`~witan_core.remote.oidc.discover_endpoints`
the device grant later relies on, including its issuer-match check). A wrong
issuer is then a clear error about the issuer, before anything is written.
``--no-verify`` skips the network call for offline/air-gapped setup.

Blocks are *appended as text* rather than round-tripped through a TOML writer:
the shipped config.toml is almost entirely comments documenting every key, and
re-serialising the parsed document would silently delete all of them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import cyclopts
import tomli_w

from .. import config as cfg_module
from ._common import _split_csv, app, console, print_error, render_table

targets_app = cyclopts.App(
    name="target",
    help="Register and inspect named [targets.*] blocks (deployed witan endpoints).",
)
app.command(targets_app, name="target")

# A target name is written into a TOML table header as a bare key. Restricting
# it to the bare-key charset keeps `[targets.<name>]` unambiguous and keeps the
# textual add/remove below from having to reason about quoting.
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

# Ordered so a rendered block reads the way the docs introduce these: what to
# talk to, how to authenticate, then which repos should route here.
_FIELD_ORDER = (
    "remote_url",
    "oidc_issuer",
    "oidc_client_id",
    "oidc_audience",
    "server",
    "graph",
    "code_server",
    "code_transport",
    "author",
    "agent",
    "match_orgs",
    "match_repos",
    "match_hosts",
    "match_paths",
)


def _toml_scalar(value: object) -> str:
    """Serialise one scalar the way TOML wants it, borrowing tomli_w's escaping."""
    return tomli_w.dumps({"x": value}).strip().removeprefix("x = ")


def _toml_value(key: str, value: object) -> str:
    """Serialise one key/value as a TOML line.

    Lists are emitted inline (``match_orgs = ["a", "b"]``) rather than in
    tomli_w's one-element-per-line style, which it uses even for a single
    entry — the match lists are short, and the surrounding file writes them
    inline. Only ever used a line at a time; see the module docstring on why
    the file as a whole is never re-serialised.
    """
    if isinstance(value, list):
        return f"{key} = [{', '.join(_toml_scalar(v) for v in value)}]"
    return f"{key} = {_toml_scalar(value)}"


def render_target_block(name: str, fields: dict[str, object]) -> str:
    """Render a ``[targets.<name>]`` table. Empty/None values are omitted."""
    lines = [f"[targets.{name}]"]
    lines.extend(
        _toml_value(key, fields[key])
        for key in _FIELD_ORDER
        if fields.get(key) not in (None, "", [])
    )
    return "\n".join(lines) + "\n"


def _header_re(name: str) -> re.Pattern[str]:
    """Match a ``[targets.<name>]`` header in any of TOML's spellings.

    TOML allows whitespace around the dotted key and lets each part be a bare
    key, a basic string, or a literal string — ``[targets.hosted]``,
    ``[targets."hosted"]``, and ``[ targets.'hosted' ]`` all declare the same
    table. Missing one spelling is not a cosmetic gap: ``add --force`` would
    fail to find the block it means to replace and append a *second*
    ``[targets.<name>]`` table, which is a duplicate-key TOML error that
    breaks every later ``witan`` command.
    """
    quoted = re.escape(name)
    return re.compile(
        rf"^\s*\[\s*targets\s*\.\s*(?:{quoted}|\"{quoted}\"|'{quoted}')\s*\]"
    )


def _is_filler(line: str) -> bool:
    """A blank or comment line — belongs to whatever table comes next."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def find_target_block(lines: list[str], name: str) -> tuple[int, int] | None:
    """Line span ``[start, end)`` of the ``[targets.<name>]`` table, or None.

    The table runs from its header to the next table header (or EOF), minus
    the trailing blank/comment run — those comments introduce whatever table
    comes *next*, so swallowing them would delete the documentation of an
    unrelated section (the shipped config.toml puts a ``# ── [rank] …`` banner
    directly above ``[rank]``).
    """
    header = _header_re(name)
    for start, line in enumerate(lines):
        if not header.match(line):
            continue
        end = start + 1
        while end < len(lines) and not lines[end].lstrip().startswith("["):
            end += 1
        while end > start + 1 and _is_filler(lines[end - 1]):
            end -= 1
        return start, end
    return None


def remove_target_block(text: str, name: str) -> tuple[str, bool]:
    """Excise the ``[targets.<name>]`` table from ``text``.

    Returns ``(new_text, removed)``. Drops exactly the table's extent —
    nothing outside it, so surrounding comments and other targets survive.
    """
    lines = text.splitlines(keepends=True)
    span = find_target_block(lines, name)
    if span is None:
        return text, False
    start, end = span
    head, tail = lines[:start], lines[end:]
    # Collapse the blank run the removed block leaves behind, so repeated
    # add/remove cycles don't grow the file.
    while head and not head[-1].strip():
        head.pop()
    while tail and not tail[0].strip():
        tail.pop(0)
    if head and tail:
        head.append("\n")
    return "".join(head) + "".join(tail), True


def replace_target_block(text: str, name: str, block: str) -> tuple[str, bool]:
    """Swap the ``[targets.<name>]`` table for ``block``, in place.

    In place, not remove-then-append: ``match_target`` returns the *first*
    matching target, so moving a block to the end of the file would silently
    re-order routing precedence for repos that more than one target matches.
    """
    lines = text.splitlines(keepends=True)
    span = find_target_block(lines, name)
    if span is None:
        return text, False
    start, end = span
    return "".join(lines[:start]) + block + "".join(lines[end:]), True


def _write_config(path: Path, text: str) -> None:
    """Replace ``path``'s contents atomically, as UTF-8.

    Atomic because this is the one place that REWRITES a file the user owns
    (``install_default_config`` only ever creates a missing one). A plain
    ``write_text`` truncates first, so a crash mid-write leaves a half-written
    config.toml — and ``load_toml`` raises on a TOML decode error, so that
    bricks every later ``witan`` command and takes the user's hand-written
    settings with it. Writing a sibling temp file and ``os.replace``-ing it
    means a reader sees either the old file or the new one, never a partial.

    UTF-8 explicitly, not the platform locale: TOML is defined as UTF-8, the
    reader already decodes it as such (``load_toml`` opens in binary and hands
    bytes to ``tomllib``), and ``default_config_toml()`` is full of non-ASCII
    box-drawing characters that a locale codec like cp1252 cannot encode.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _existing_names() -> list[str]:
    """Names of the currently-configured targets, or a clean exit on a bad file."""
    try:
        return [t.name for t in cfg_module._parse_targets(cfg_module._load_toml())]
    except ValueError as exc:
        print_error(exc)
        raise SystemExit(1) from None


def _verify_issuer(issuer: str) -> None:
    """Fail the command if ``issuer`` is not a usable OIDC device-grant issuer."""
    from witan_core.remote.oidc import RemoteAuthError, discover_endpoints

    try:
        discover_endpoints(issuer)
    except RemoteAuthError as exc:
        console.print(
            f"[red]Could not verify OIDC issuer[/red] {issuer}\n  {exc}\n"
            "  Nothing was written. Fix the issuer, or pass [bold]--no-verify[/bold] "
            "to register it anyway (e.g. when offline)."
        )
        raise SystemExit(1) from None


@targets_app.command
def add(
    name: str,
    *,
    remote_url: str | None = None,
    oidc_issuer: str | None = None,
    oidc_client_id: str | None = None,
    oidc_audience: str | None = None,
    server: str | None = None,
    graph: str | None = None,
    code_server: str | None = None,
    code_transport: Literal["direct", "mcp"] | None = None,
    author: str | None = None,
    agent: str | None = None,
    match_orgs: list[str] | None = None,
    match_repos: list[str] | None = None,
    match_hosts: list[str] | None = None,
    match_paths: list[str] | None = None,
    force: bool = False,
    verify: bool = True,
    login: bool = False,
    dry_run: bool = False,
) -> None:
    """Register a named target — a deployed witan endpoint, or a local store.

    Writes a ``[targets.<name>]`` block to the config file in effect
    (``WITAN_CONFIG``, else ``~/.config/witan/config.toml``), creating a
    starter config first if none exists. Comments in an existing file are
    preserved: the block is appended, not re-serialised.

    Joining a deployment is then::

        witan target add hosted \\
            --remote-url https://witan.example.org/mcp \\
            --oidc-issuer https://sso.example.org/realms/ol-platform-engineering \\
            --match-orgs my-org
        witan login --target hosted
        witan whoami --target hosted

    That registers code graphs on the deployment too: ``--remote-url`` implies
    ``code_transport = "mcp"``, so branches you index are visible to the rest of
    the team rather than staying on this machine. Pass
    ``--code-transport direct`` (with ``--code-server``) for an in-cluster
    writer such as the CI indexer.

    Passing ``--match-orgs``/``--match-repos``/``--match-hosts``/``--match-paths``
    lets the target select itself for matching checkouts, so ``--target`` is not
    needed after the first time. Without any of them the target is only ever
    reached explicitly (``--target``/``WITAN_TARGET``).

    Parameters
    ----------
    name: Target name — the ``<name>`` in ``[targets.<name>]``.
    remote_url: Deployed witan MCP endpoint, e.g. https://witan.example.org/mcp.
    oidc_issuer: OIDC realm issuer minting its tokens; required with --remote-url.
    oidc_client_id: Public OIDC device-grant client id (default ``witan-cli``).
    oidc_audience: Expected JWT audience, matching the deployment's audience.
    server: omnigraph store URI or server URL, for a local/self-hosted target.
    graph: omnigraph graph id addressed on ``server``.
    code_server: omnigraph-server URL holding this target's code graphs.
        The DATA tier, reachable from inside the cluster only — distinct from
        ``--server``, which addresses the memory graph.
    code_transport: How code-graph writes reach the cluster.
        ``mcp`` routes them through the deployed witan endpoint and is the
        default alongside ``--remote-url``; ``direct`` addresses
        ``--code-server`` and only works from inside the cluster.
    author: Attribution written to graph nodes under this target.
    agent: Default agent CLI for ``witan run`` under this target.
    match_orgs: Repo orgs that should route here (repeatable, or comma-separated).
    match_repos: Repo URIs/paths that should route here.
    match_hosts: Repo hosts that should route here.
    match_paths: Local checkout path prefixes that should route here.
    force: Replace an existing block of the same name instead of refusing.
    verify: Check the OIDC issuer's discovery document before writing.
    login: Run the device-grant login against the new target once written.
    dry_run: Print the block that would be written and exit.
    """
    if not _BARE_KEY.match(name):
        console.print(
            f"[red]Invalid target name[/red] {name!r} — use letters, digits, "
            "underscores, and dashes only."
        )
        raise SystemExit(1)

    if not remote_url and not server:
        console.print(
            "[red]Nothing to register.[/red] Pass [bold]--remote-url[/bold] for a "
            "deployed witan, or [bold]--server[/bold] for a local/self-hosted store."
        )
        raise SystemExit(1)

    # Mirrors resolve_remote_config()'s own invariant, but reports it now rather
    # than on the next command that tries to use the target.
    if remote_url and not oidc_issuer:
        console.print(
            "[red]--remote-url needs --oidc-issuer.[/red] The CLI authenticates to a "
            "deployed witan with a per-user OIDC token; without an issuer it has "
            "nowhere to get one."
        )
        raise SystemExit(1)

    # Caught here rather than after writing: `witan login` on a store-only
    # target exits 1 with "remote mode is not configured", which would report
    # a perfectly successful registration as a failure.
    if login and not remote_url:
        console.print(
            "[red]--login needs --remote-url.[/red] There is no device grant to run "
            "against a local/self-hosted [bold]--server[/bold] target."
        )
        raise SystemExit(1)

    # ── Code graphs, which are routed SEPARATELY from memory ─────────────────
    #
    # `remote_url` routes the memory/work graph; `code_transport` routes the
    # code graphs, and nothing tied them together. A block written without this
    # gave you memory on the deployment and code graphs in a directory on one
    # laptop — the state ADR-0005 rejected outright when it chose to route
    # writes through the MCP tier ("in-cluster-only indexing was rejected
    # because it gives up the per-developer branch views ADR-0006 built").
    #
    # So a deployed target defaults to `mcp` rather than inheriting the global
    # `direct`. `direct` is for writers that share the cluster network (the CI
    # indexer, maintenance jobs); anyone registering a `--remote-url` is by
    # definition outside it, and for them `direct` fails as an unreachable host
    # rather than as a configuration error. Written as an explicit key rather
    # than left to a changed default, so the global default keeps meaning what
    # the in-cluster writers need.
    if code_transport is None and remote_url:
        code_transport = "mcp"

    if code_transport == "direct" and not (code_server or server):
        console.print(
            "[red]--code-transport direct needs --code-server.[/red] Direct means "
            "addressing the omnigraph-server holding the code graphs, which is "
            "ClusterIP-only; from outside the cluster use "
            "[bold]--code-transport mcp[/bold] instead."
        )
        raise SystemExit(1)

    if code_transport == "mcp" and not remote_url:
        console.print(
            "[red]--code-transport mcp needs --remote-url.[/red] mcp routes code-graph "
            "writes through the deployed witan endpoint, so there has to be one."
        )
        raise SystemExit(1)

    existing = _existing_names()
    if name in existing and not force:
        console.print(
            f"[red]Target {name!r} already exists[/red] in {cfg_module.config_path()}. "
            "Pass [bold]--force[/bold] to replace it, or pick another name."
        )
        raise SystemExit(1)

    fields: dict[str, object] = {
        "remote_url": remote_url,
        "oidc_issuer": oidc_issuer,
        "oidc_client_id": oidc_client_id,
        "oidc_audience": oidc_audience,
        "server": server,
        "graph": graph,
        "code_server": code_server,
        "code_transport": code_transport,
        "author": author,
        "agent": agent,
        "match_orgs": _split_csv(match_orgs),
        "match_repos": _split_csv(match_repos),
        "match_hosts": _split_csv(match_hosts),
        "match_paths": _split_csv(match_paths),
    }
    block = render_target_block(name, fields)

    if dry_run:
        console.print(f"[dim]→ {cfg_module.config_path()}[/dim]\n")
        # markup=False: a `[targets.x]` header is valid Rich markup and would
        # otherwise be parsed as a style tag and vanish from the output.
        console.print(block.rstrip(), markup=False)
        console.print("\n[dim](dry-run — nothing written)[/dim]")
        return

    if oidc_issuer and verify:
        _verify_issuer(oidc_issuer)

    path = cfg_module.config_path()
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = cfg_module.default_config_toml()

    replacing = name in existing
    if replacing:
        text, found = replace_target_block(text, name, block)
        if not found:
            # The reader saw the table but the text scan did not — an exotic
            # header spelling. Appending anyway would declare the table twice
            # and leave the file unparseable, so refuse instead.
            console.print(
                # \\[ escapes the bracket: Rich would read `[targets.x]` as a
                # style tag and swallow it.
                f"[red]Could not locate the \\[targets.{name}] block[/red] in {path} "
                "to replace it — its header is written in a form this command cannot "
                "rewrite safely. Edit or delete it by hand, then re-run."
            )
            raise SystemExit(1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text = f"{text}\n{block}"
    _write_config(path, text)

    verb = "Replaced" if replacing else "Registered"
    console.print(f"[green]{verb} target[/green] [bold]{name}[/bold] → {path}")

    if login:
        from .auth import login as login_cmd

        login_cmd(target=name)
    elif remote_url:
        console.print(
            f"  Next: [bold]witan login --target {name}[/bold], then "
            f"[bold]witan whoami --target {name}[/bold] to confirm."
        )


@targets_app.command(name="list")
def list_targets() -> None:
    """List configured targets, marking the one in effect here with ``*``."""
    from witan_core.target_config import local_project_path, match_target

    from .. import repo as repo_module

    try:
        targets = cfg_module._parse_targets(cfg_module._load_toml())
    except ValueError as exc:
        print_error(exc)
        raise SystemExit(1) from None
    if not targets:
        console.print(
            f"[yellow]No targets configured[/yellow] in {cfg_module.config_path()}.\n"
            "  Add one with [bold]witan target add <name> --remote-url … "
            "--oidc-issuer …[/bold]"
        )
        return

    # Same precedence load_remote_config()/load() use: WITAN_TARGET pins a
    # target outright, and auto-detection only runs when it is unset. Marking
    # the repo-matched one regardless would point `*` at a target no other
    # command is going to use.
    if pinned := os.environ.get("WITAN_TARGET"):
        selected = next((t for t in targets if t.name == pinned), None)
        if selected is None:
            console.print(
                f"[yellow]WITAN_TARGET={pinned!r} is not a configured target[/yellow] "
                "— no target is in effect."
            )
    else:
        selected = match_target(
            targets, repo_uri=repo_module.detect(), local_path=local_project_path()
        )
    rows = [
        {
            "cur": "*" if selected is not None and t.name == selected.name else "",
            "name": t.name,
            "remote_url": t.remote_url,
            "server": t.server,
            "graph": t.graph,
            "matches": ", ".join(
                t.match_orgs + t.match_repos + t.match_hosts + t.match_paths
            ),
        }
        for t in targets
    ]
    render_table(
        title=f"witan targets ({cfg_module.config_path()})",
        columns=["cur", "name", "remote_url", "server", "graph", "matches"],
        rows=rows,
        no_wrap={"cur", "name"},
        placeholders={"matches": "explicit only"},
    )


@targets_app.command
def remove(name: str, *, dry_run: bool = False) -> None:
    """Delete a ``[targets.<name>]`` block from the config file.

    Parameters
    ----------
    name: Target to remove.
    dry_run: Report what would be removed without writing.
    """
    path = cfg_module.config_path()
    if not path.exists():
        console.print(f"[yellow]No config file[/yellow] at {path} — nothing to remove.")
        raise SystemExit(1)

    text = path.read_text(encoding="utf-8")
    new_text, removed = remove_target_block(text, name)
    if not removed:
        available = ", ".join(_existing_names()) or "(none defined)"
        console.print(
            f"[red]No target {name!r}[/red] in {path}. Available: {available}"
        )
        raise SystemExit(1)

    if dry_run:
        console.print(
            f"[dim]Would remove target[/dim] [bold]{name}[/bold] from {path} "
            "[dim](dry-run)[/dim]"
        )
        return

    _write_config(path, new_text)
    console.print(f"[green]Removed target[/green] [bold]{name}[/bold] from {path}")
