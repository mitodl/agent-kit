"""witan-code's plugin for the witan language server (ADR 0012).

Registered under the ``witan.lsp.plugins`` entry-point group. It routes each
workspace folder the way ``witan-code`` would from that folder, so it reads
the same graph: a ``RemoteServerProxy`` when the folder's target names a
deployment, else the in-process server reading that target's local stores.

Every read names the folder's repo and passes the folder's checkout branch as
``branch``. One process serves several checkouts, so nothing may follow the
process's own (the in-process server is told so in :meth:`CodePlugin.open`),
and no tool is left with an argument it would ask about.

The features, from ADR 0012's feature map:

- Symbol resolution: the innermost symbol around a line, from
  ``code_symbols_in_file``. witan-council's symbol-scoped features ask for it
  through the host's ``Context.symbol_at``.
- A callers lens on each function, method and class, and a cross-repo impact
  lens on each symbol with interface bindings. Both resolve lazily, and their
  commands return the rows behind the count (see :meth:`execute_command`).
  Where the client has no CodeLens the host moves them into hover and code
  actions.
- Hover on a line holding an env var, endpoint, package or service binding:
  who provides and who consumes it, with repo, file and line.
- Definition across indexed repos, and references within the definition's
  repo (each repo's graph holds only its own edges). A location in a repo
  that no workspace folder has checked out cannot be opened, so the hover
  names it instead (repo, path and line).
- One Information diagnostic on line 0 when the folder's repo, or its branch,
  has no index.

A deployment that refuses or cannot be reached fails the request with the
reason. Nothing prompts: an expired or missing ``witan login`` token makes the
token provider raise ``NeedsLogin`` rather than start a device flow, and the
host leaves stdin (the JSON-RPC channel) unreadable to prompts. Hover, lenses
and diagnostics are merged with witan-council's, so those show the reason (or
nothing) instead of failing, and the council sections still arrive.

Nothing here imports pygls, so the module loads without the ``lsp`` extra.
"""

from __future__ import annotations

import builtins
import keyword
import os
import re
import subprocess
import time
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from witan_core.lsp import (
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
from witan_core.remote.oidc import RemoteAuthError
from witan_core.remote.proxy import (
    RemoteCredentialRejected,
    RemoteToolFailed,
    RemoteToolUnavailable,
    RemoteUnreachable,
)
from witan_core.repo_key import checkout_branch

from . import config as cfg_module
from . import repo as repo_module

__all__ = ["CALLERS_COMMAND", "IMPACT_COMMAND", "CodePlugin"]

CALLERS_COMMAND = "witan.listCallers"
IMPACT_COMMAND = "witan.listCrossRepoImpact"

_LENS_KINDS = frozenset({"function", "method", "class"})
"""Symbol kinds that get a callers lens. A variable or a YAML key has none."""

_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")

_MAX_LISTED = 20
"""Rows one hover list shows before it says how many more there are."""

_MAX_TARGETS = 10
"""Definitions of one name whose references a request reads. A bare name like
``run`` can match dozens of symbols, each one more read."""

_CALLERS_LIMIT = 200
"""The ``callers`` query's row limit (``code_read.gq``): a full page means at
least this many."""

_UNLIKELY_NAMES = frozenset(
    [*keyword.kwlist, *keyword.softkwlist, *dir(builtins), "self", "cls"]
)
"""Names not worth a lookup across every indexed repo when the folder's own
repo has no definition: hovering ``self`` or ``len`` would otherwise read
every store."""

_BRANCH_TTL_SECONDS = 2.0
"""How long a folder's checkout branch is trusted before git is asked again,
so a ``git switch`` reaches the next read without a git call per read."""

_UNAVAILABLE = (
    RemoteAuthError,
    RemoteCredentialRejected,
    RemoteToolFailed,
    RemoteToolUnavailable,
    RemoteUnreachable,
)
"""Why a deployment cannot answer: not logged in, a rejected token, a refused
or failed call, an older deployment without a tool, or no connection. Each
carries a message that says what to do."""


def _config(folder: Folder) -> cfg_module.Config:
    """The config ``witan-code`` run from ``folder`` would use. A relative
    ``code_dir`` is relative to the cwd, which for the CLI is the folder."""
    cfg = cfg_module.load(folder.target, project_path=folder.path)
    return replace(cfg, code_dir=folder.path / cfg.code_dir)


def _path(uri: str) -> Path:
    return Path(url2pathname(urlparse(uri).path))


def _git_root(path: Path) -> Path | None:
    """The checkout root above ``path``, spelled through ``path`` itself.

    ``--show-cdup`` rather than ``--show-toplevel``, which resolves symlinks:
    a root reached through a symlinked folder would turn every location into
    a URI the editor has open under a different name.
    """
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-cdup"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(os.path.normpath(path / result.stdout.strip()))


def _rel(symbol: dict) -> str:
    """A symbol row's repo-relative path, from its ``repo#path`` file id."""
    return symbol["file_id"].split("#", 1)[1]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _word_at(text: str, line: int, character: int) -> str | None:
    # LSP lines end at \n, \r\n or \r only; str.splitlines also splits on a
    # form feed, which Emacs-edited Python files carry.
    lines = re.split(r"\r\n|\r|\n", text)
    if line >= len(lines):
        return None
    for match in _IDENTIFIER.finditer(lines[line]):
        if match.start() <= character <= match.end():
            return match.group()
    return None


class CodePlugin(Plugin):
    capabilities = frozenset(
        {
            Capability.SYMBOL_RESOLUTION,
            Capability.CODE_LENS,
            Capability.HOVER,
            Capability.DEFINITION,
            Capability.REFERENCES,
            Capability.DIAGNOSTICS,
        }
    )
    commands = (CALLERS_COMMAND, IMPACT_COMMAND)
    tools = frozenset(
        {
            "code_symbols_in_file",
            "code_callers",
            "code_cross_repo_impact",
            "code_interface_bindings_in_file",
            "code_interface_providers",
            "code_interface_consumers",
            "code_find_definition",
            "code_find_references",
            "code_indexed_branches",
        }
    )

    def __init__(self) -> None:
        self._roots: dict[Path, Path | None] = {}
        self._branches: dict[Path, tuple[float, str | None]] = {}

    # ── routing ───────────────────────────────────────────────────────────

    def route(self, folder: Folder) -> Route:
        remote = cfg_module.load_remote_config(folder.target, project_path=folder.path)
        if remote is not None:
            return Route(key=remote, local=False)
        cfg = _config(folder)
        return Route(key=(str(cfg.code_dir), cfg.code_server), local=True)

    def open(self, route: Route, folder: Folder) -> Any:
        if not route.local:
            from .cli import remote_proxy

            return remote_proxy(route.key)
        from . import server as server_module

        server_module.use_config(_config(folder), follow_checkout=False)
        return server_module

    # ── where things are ──────────────────────────────────────────────────

    def _root(self, folder: Folder) -> Path | None:
        if folder.path not in self._roots:
            self._roots[folder.path] = _git_root(folder.path)
        return self._roots[folder.path]

    def _branch(self, folder: Folder) -> str | None:
        """The ``branch`` to read ``folder``'s checkout at now, or ``None``
        for the store's main view.

        ``None`` on the repo's default branch and on a detached HEAD, decided
        the way indexing decides it (``repo.store_branch``). Naming the
        default branch instead would be read as a git branch and mapped
        through ``branch_store_name``, which sends ``main`` to the ``_main``
        view a git branch named ``_main`` indexes onto. ``Folder.branch`` is
        not used: it is what was checked out when the editor reported the
        folder.
        """
        stamp, branch = self._branches.get(folder.path, (0.0, None))
        if time.monotonic() - stamp >= _BRANCH_TTL_SECONDS:
            on_main = repo_module.store_branch(folder.path) is None
            branch = None if on_main else checkout_branch(folder.path)
            self._branches[folder.path] = (time.monotonic(), branch)
        return branch

    def _document(self, ctx: Context, uri: str) -> str | None:
        """``uri``'s path relative to its checkout root, as the graph names
        files. ``None`` when the folder has no repo to read."""
        root = self._root(ctx.folder)
        path = _path(uri)
        if ctx.folder.repo is None or root is None or not path.is_relative_to(root):
            return None
        return path.relative_to(root).as_posix()

    @staticmethod
    def _folder_of(ctx: Context, repo: str) -> Folder | None:
        """The workspace folder that has ``repo`` checked out, this one first."""
        if ctx.folder.repo == repo:
            return ctx.folder
        return next((f for f in ctx.folders if f.repo == repo), None)

    def _branch_of(self, ctx: Context, repo: str) -> str | None:
        """The branch to read ``repo`` at: its open folder's. A repo nobody
        has open reads its default view."""
        folder = self._folder_of(ctx, repo)
        return self._branch(folder) if folder else None

    def _location(
        self, ctx: Context, repo: str, rel: str, line: int | None
    ) -> Location | None:
        """Where a graph row is in this workspace (``line`` 1-based, as the
        graph stores it), or ``None`` when no folder has its repo."""
        folder = self._folder_of(ctx, repo)
        root = self._root(folder) if folder else None
        if root is None:
            return None
        return Location(uri=(root / rel).as_uri(), line=max((line or 1) - 1, 0))

    def _listed(
        self, ctx: Context, rows: Sequence[tuple[str, str, int | None, str]]
    ) -> list[str]:
        """Markdown list items for ``(repo, path, line, note)`` rows, linked
        when the repo is checked out here."""
        items = []
        for repo, rel, line, note in rows[:_MAX_LISTED]:
            where = f"{repo} {rel}:{line}" if line else f"{repo} {rel}"
            text = escape_markdown(f"{where} {note}" if note else where)
            loc = self._location(ctx, repo, rel, line)
            items.append(
                f"- [{text}]({loc.uri}#L{loc.line + 1})" if loc else f"- {text}"
            )
        if len(rows) > _MAX_LISTED:
            items.append(f"- and {len(rows) - _MAX_LISTED} more")
        return items or ["- none indexed"]

    def _item(
        self, ctx: Context, title: str, repo: str, rel: str, line: int | None
    ) -> dict:
        loc = self._location(ctx, repo, rel, line)
        return {
            "title": title,
            "repo": repo,
            "path": rel,
            "line": line,
            "uri": loc.uri if loc else None,
        }

    # ── reads ─────────────────────────────────────────────────────────────

    def _symbols(self, ctx: Context, uri: str) -> list[dict]:
        rel = self._document(ctx, uri)
        if rel is None:
            return []
        return ctx.tools.code_symbols_in_file(
            path=rel, repo=ctx.folder.repo, branch=self._branch(ctx.folder)
        )

    def _bindings(self, ctx: Context, uri: str) -> list[dict]:
        rel = self._document(ctx, uri)
        if rel is None:
            return []
        return ctx.tools.code_interface_bindings_in_file(
            path=rel, repo=ctx.folder.repo, branch=self._branch(ctx.folder)
        )

    def _targets(self, ctx: Context, uri: str, line: int, character: int) -> list[dict]:
        """Definitions of the identifier under the cursor: in the folder's own
        repo at its branch, else in the other open repos at their branches and
        in every repo no folder has open at its main view."""
        word = _word_at(ctx.text(uri), line, character)
        if word is None or self._document(ctx, uri) is None:
            return []
        folder = ctx.folder
        own = ctx.tools.code_find_definition(
            name=word, repo=folder.repo, branch=self._branch(folder)
        )
        if own or word in _UNLIKELY_NAMES:
            return own
        others = {}
        for f in ctx.folders:
            if f.repo is not None and f.repo != folder.repo:
                others.setdefault(f.repo, f)
        found = [
            row
            for repo, f in others.items()
            for row in ctx.tools.code_find_definition(
                name=word, repo=repo, branch=self._branch(f)
            )
        ]
        return found + [
            row
            for row in ctx.tools.code_find_definition(name=word)
            if row["repo"] not in others
        ]

    def _references(self, ctx: Context, target: dict) -> list[dict]:
        return ctx.tools.code_find_references(
            symbol_id=target["symbol_id"], branch=self._branch_of(ctx, target["repo"])
        )

    # ── features ──────────────────────────────────────────────────────────

    def resolve_symbol(
        self, ctx: Context, uri: str, line: int, character: int
    ) -> str | None:
        """The innermost symbol whose lines hold ``line``; the module symbol
        when no definition does."""
        try:
            symbols = self._symbols(ctx, uri)
        except _UNAVAILABLE:
            return None  # this plugin's hover says why
        around = [s for s in symbols if s["start_line"] <= line + 1 <= s["end_line"]]
        best = min(
            around,
            key=lambda s: (s["kind"] == "module", s["end_line"] - s["start_line"]),
            default=None,
        )
        return best["symbol_id"] if best else None

    def code_lenses(self, ctx: Context, uri: str) -> list[Lens]:
        try:
            symbols = self._symbols(ctx, uri)
            bound = {
                b["symbol_id"] for b in self._bindings(ctx, uri) if not b["generic"]
            }
        except _UNAVAILABLE:
            return []  # the diagnostic and the hover say why
        lenses = []
        for symbol in symbols:
            line, sid = symbol["start_line"] - 1, symbol["symbol_id"]
            if symbol["kind"] in _LENS_KINDS:
                lenses.append(Lens(line=line, data={"lens": "callers", "symbol": sid}))
            if sid in bound:
                lenses.append(Lens(line=line, data={"lens": "impact", "symbol": sid}))
        return lenses

    def resolve_lens(self, ctx: Context, uri: str, lens: Lens) -> Lens:
        sid, folder = lens.data["symbol"], ctx.folder
        branch = self._branch(folder)
        arguments = (folder.uri, sid)
        if lens.data["lens"] == "callers":
            count = len(ctx.tools.code_callers(symbol_id=sid, branch=branch))
            return replace(
                lens,
                title=(
                    f"{count}+ callers"
                    if count >= _CALLERS_LIMIT
                    else _plural(count, "caller")
                ),
                command=CALLERS_COMMAND,
                arguments=arguments,
            )
        cross = ctx.tools.code_cross_repo_impact(symbol_id=sid, branch=branch)[
            "cross_repo"
        ]
        repos = {b["repo"] for b in cross}
        title = (
            f"{_plural(len(cross), 'cross-repo binding')} in {_plural(len(repos), 'repo')}"
            if cross
            else "no cross-repo bindings"
        )
        return replace(lens, title=title, command=IMPACT_COMMAND, arguments=arguments)

    def execute_command(
        self, ctx: CommandContext, command: str, arguments: Sequence[Any]
    ) -> list[dict]:
        """The rows behind a lens, for the client to list.

        ``arguments`` is the lens's ``(folder uri, symbol id)``. Each row is
        ``{title, repo, path, line, uri}``: ``line`` 1-based, as the graph
        stores it (``null`` for a binding with no single line), and ``uri``
        the file in this workspace, or ``null`` when no folder has the repo
        checked out.
        """
        folder_uri, symbol_id = arguments
        folder = {f.uri: f for f in ctx.folders}.get(folder_uri)
        if folder is None:
            raise ValueError("witan: this folder is no longer served")
        fctx = ctx.context(folder)
        if fctx is None:
            raise ValueError(
                f"witan-code serves no code graph for {folder.path}; see its "
                "diagnostic."
            )
        if command == CALLERS_COMMAND:
            callers = fctx.tools.code_callers(
                symbol_id=symbol_id, branch=self._branch(folder)
            )
            return [
                self._item(
                    fctx, c["qualified_name"], c["repo"], _rel(c), c["start_line"]
                )
                for c in callers
            ]
        impact = fctx.tools.code_cross_repo_impact(
            symbol_id=symbol_id, branch=self._branch(folder)
        )
        return [
            self._item(
                fctx,
                f"{b['role']} of {b['kind']} {b['key']}",
                b["repo"],
                b["file"],
                b["line"],
            )
            for b in impact["cross_repo"]
        ]

    def hover(self, ctx: Context, uri: str, line: int, character: int) -> str | None:
        try:
            sections = [
                *self._contract_sections(ctx, uri, line),
                *self._elsewhere_sections(ctx, uri, line, character),
            ]
        except _UNAVAILABLE as exc:
            return f"witan-code: {escape_markdown(str(exc))}"
        return "\n\n".join(sections) or None

    def _contract_sections(self, ctx: Context, uri: str, line: int) -> Iterator[str]:
        """Providers and consumers of each contract bound on ``line``."""
        seen = set()
        for b in self._bindings(ctx, uri):
            contract = (b["kind"], b["key_norm"])
            if b["line"] != line + 1 or contract in seen:
                continue
            seen.add(contract)
            sides = []
            for heading, tool in (
                ("Providers", ctx.tools.code_interface_providers),
                ("Consumers", ctx.tools.code_interface_consumers),
            ):
                rows = [
                    (r["repo"], r["file"], r["line"], "")
                    for r in tool(kind=b["kind"], key=b["key"])
                ]
                sides.append(f"{heading}:\n\n" + "\n".join(self._listed(ctx, rows)))
            yield "\n\n".join([f"**{b['kind']}** {escape_markdown(b['key'])}", *sides])

    def _elsewhere_sections(
        self, ctx: Context, uri: str, line: int, character: int
    ) -> Iterator[str]:
        """Definitions under the cursor that no workspace folder has checked
        out, and their references: what ``definition`` and ``references``
        cannot return as a location."""
        away = [
            t
            for t in self._targets(ctx, uri, line, character)
            if self._location(ctx, t["repo"], _rel(t), t["start_line"]) is None
        ]
        if not away:
            return
        yield "Defined in a repo not open here:\n\n" + "\n".join(
            self._listed(
                ctx,
                [
                    (t["repo"], _rel(t), t["start_line"], t["qualified_name"])
                    for t in away
                ],
            )
        )
        refs = [
            (r["repo"], _rel(r), r["start_line"], r["qualified_name"])
            for t in away[:_MAX_TARGETS]
            for r in self._references(ctx, t)
        ]
        if refs:
            yield "Referenced from:\n\n" + "\n".join(self._listed(ctx, refs))

    def definition(
        self, ctx: Context, uri: str, line: int, character: int
    ) -> list[Location]:
        return [
            loc
            for t in self._targets(ctx, uri, line, character)
            if (loc := self._location(ctx, t["repo"], _rel(t), t["start_line"]))
        ]

    def references(
        self,
        ctx: Context,
        uri: str,
        line: int,
        character: int,
        include_declaration: bool,
    ) -> list[Location]:
        targets = self._targets(ctx, uri, line, character)[:_MAX_TARGETS]
        rows = [r for t in targets for r in self._references(ctx, t)]
        if include_declaration:
            rows = targets + rows
        locations = [
            loc
            for r in rows
            if (loc := self._location(ctx, r["repo"], _rel(r), r["start_line"]))
        ]
        return list(dict.fromkeys(locations))

    def diagnostics(self, ctx: Context, uri: str) -> list[Note]:
        folder = ctx.folder
        if self._document(ctx, uri) is None:
            return []
        try:
            branch = self._branch(folder)
            rows = ctx.tools.code_indexed_branches(branch=branch)
        except _UNAVAILABLE as exc:
            return [Note(line=0, message=f"code features unavailable: {exc}")]
        row = next((r for r in rows if r["repo"] == folder.repo), None)
        if row is None:
            return [
                Note(
                    line=0,
                    message=(
                        f"{folder.repo} has no code index, so code "
                        "features find nothing here. Run `witan-code index` in "
                        "this checkout."
                    ),
                )
            ]
        # The default branch is the store's main view, which exists with the
        # store; a detached HEAD reads main. Neither has a view to look for.
        if branch is None:
            return []
        if row["views"] is None or row["views"]:
            return []
        return [
            Note(
                line=0,
                message=(
                    f"branch {branch} has no code index, so "
                    f"code features show {folder.repo}'s default branch. Run "
                    "`witan-code index` in this checkout to index it."
                ),
            )
        ]
