"""witan-council's plugin for the witan language server (ADR 0012).

Registered under the ``witan.lsp.plugins`` entry-point group. It routes each
workspace folder through the CLI's own dispatch, so the features read the same
graph ``witan`` would from that folder: a ``RemoteServerProxy`` when the
folder's target names a deployment, else the in-process server behind
``local_server``'s guard.

The features, per ADR 0012's feature map:

- an Information diagnostic on the first line of a file a memory's stale
  ``symbol_refs`` names (``memory_stale_refs``);
- a hover section on a symbol listing the tasks and memories whose
  ``symbol_refs`` name it (``recall(symbol_id=...)``), only when another
  plugin resolves positions to symbols;
- the ``witan.*`` commands below, whose results are JSON a client renders.

Every command takes at most one argument, an object. Its ``uri`` names the
workspace folder the command applies to, or any file in one (the innermost
folder holding it wins); without it the first folder is used. The list
commands answer ``{"repo": ..., <list>: [...]}`` for that folder's repo:

- ``witan.listStaleRefs``: ``refs``, one row per ref that is not live, with
  ``uri`` ``null`` when the file it names is gone.
- ``witan.listReadyTasks``: ``ready`` (``task_ready``), ``for_branch``
  (``task_for_branch`` on the folder's branch) and ``branch``.
- ``witan.listProjects``: ``projects`` (``workflow_project_list``).
- ``witan.listMyWork``: ``tasks``, the caller's ``in_progress`` tasks.
- ``witan.listProjectTasks`` takes ``project``, a ``wp-`` slug, and answers
  the project's open tasks laid out in dependency waves (:mod:`witan.waves`):
  ``tasks``, each row with ``wave``, ``open_blockers``, ``outside``,
  ``cycle``, ``critical``, ``downstream`` and ``ready`` (from ``task_ready``),
  plus ``waves``, ``critical``, ``cycles`` and ``disagree``. An open blocker
  from another project is a row with ``outside`` true.
- ``witan.runTask`` / ``witan.runProject`` take ``slug`` and optional
  ``agent`` and ``model``, and answer ``build_launch_spec``'s spec or refusal.
  ``witan.runTask`` also takes ``claim`` (default true); ``false`` builds the
  unclaimed spec a chat-panel handoff needs. ``handoff`` is judged against
  the editor's environment (``CommandContext.editor_environ``), the one a
  chat-panel agent inherits. A spec that took a claim carries ``release``,
  the arguments for ``witan.releaseTask``.
- ``witan.releaseTask`` takes ``slug``, ``claimed_at`` and ``uri``, which is
  the spec's ``release`` as given, and answers ``task_release``'s result.
  ``uri`` is required and names the folder the launch was invoked from, not
  the spec's ``cwd``: the claim was taken through that folder's route, and
  ``cwd`` can be another open folder that routes to a different graph.
- ``witan.uiBundle`` takes no argument and answers ``{"path": ...}``, the
  web UI bundle this install ships, for an editor webview to load. The
  webview's reads go through the host's ``witan.uiRead``, to the web UI
  reads this plugin declares.

The three work lists (ready tasks, projects, my work) also take ``scope``:
``repo`` (the folder's repo only, the default), ``repo+unfiled`` (also what is
filed under no repo) or ``all`` (every repo). ``project``, a ``wp-`` slug,
narrows them to that one project in any repo instead; ``for_branch`` stays the
folder's own. They answer with the scope applied, ``scope`` (``"project"``
under a project) and ``project`` (the slug, or ``null``), so a client can
label what it shows. A folder with no git remote has no repo, so it answers
``repo`` ``null`` and only the ``all`` and ``project`` scopes, with an empty
``for_branch``, and ``witan.listProjectTasks``; the repo scopes and
``witan.listStaleRefs`` refuse it.

Every tool call names its scope outright (the folder's own repo, every repo or
one project), so nothing falls back to the process's cwd or asks. Every call
also runs inside :func:`prompts_off`: stdin is the JSON-RPC channel, so a tool
must never put a question to it. A missing
or expired ``witan login`` token does not prompt either: the token provider
raises ``NeedsLogin``, which fails the request with its message.

Nothing here imports pygls, so the module loads without the ``lsp`` extra.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from witan_core.lsp import (
    BOUND_TOOLS,
    Capability,
    CommandContext,
    Context,
    Folder,
    Note,
    Plugin,
    Route,
    escape_markdown,
)
from witan_core.observability import get_logger
from witan_core.repo_key import checkout_branch

from . import config as cfg_module
from . import waves
from .prompts import prompts_off
from .repo import normalise
from .ui_access import OPTIONAL_READ_TOOLS, READ_TOOLS

LIST_STALE_REFS = "witan.listStaleRefs"
LIST_READY_TASKS = "witan.listReadyTasks"
LIST_PROJECTS = "witan.listProjects"
LIST_MY_WORK = "witan.listMyWork"
RUN_TASK = "witan.runTask"
RUN_PROJECT = "witan.runProject"
RELEASE_TASK = "witan.releaseTask"
UI_BUNDLE = "witan.uiBundle"
LIST_PROJECT_TASKS = "witan.listProjectTasks"

WORK_LISTS = (LIST_READY_TASKS, LIST_PROJECTS, LIST_MY_WORK)
SCOPE_REPO = "repo"
SCOPE_REPO_UNFILED = "repo+unfiled"
SCOPE_ALL = "all"
SCOPE_PROJECT = "project"
SCOPES = (SCOPE_REPO, SCOPE_REPO_UNFILED, SCOPE_ALL)
DEFAULT_SCOPE = SCOPE_REPO
"""The editor is about the open code; unfiled work is one pick away."""

MAX_TASK_LIMIT = 10_000
"""``task_list``'s maximum, asked for where a read must be whole. Without a
limit ``task_list``'s all-repos read stops at 50 rows, before the assignee
filter runs, and ``task_ready`` at 20."""

OUTSIDE_BLOCKER_FIELDS = ("slug", "title", "type", "status", "priority")
"""What an outside blocker's stub keeps of its ``task_get`` row: the fields
``waves.layout`` and the editor's tree read. The rest of that row (comments,
``blocks``, children, branches, the description) never reaches the editor."""

STALE_REFS_TTL_SECONDS = 60
"""How long one folder's ``memory_stale_refs`` result serves its diagnostics.
The review resolves every ref in the repo against witan-code, and an editor
publishes diagnostics on every open and save."""

logger = get_logger("witan.lsp_plugin")

HOVER_LIMIT = 10
"""Tasks, and separately memories, listed in one hover."""


def _store(cfg: cfg_module.Config, folder: Folder) -> str:
    """The graph ``cfg`` names, as ``witan`` run from ``folder`` would open it.

    An omnigraph server serves many graphs, so its URL alone does not name
    one. A relative path is relative to the cwd, which for the CLI is the
    folder. Credentials are left out: the key is shown in the diagnostic on a
    refused folder, and a token does not change which graph is read.
    """
    uri = cfg.graph_uri
    if uri.startswith(("http://", "https://")):
        return f"graph {cfg.graph_name} on {uri}"
    if uri.startswith("s3://"):
        return uri
    return str(folder.path / uri)


def _uri_path(uri: str) -> Path:
    return Path(url2pathname(urlsplit(uri).path))


def _checkout_root(folder: Path) -> Path | None:
    """The top of the checkout ``folder`` is in; symbol ids are relative to it.

    git answers with the physical path, so a folder opened through a symlink
    is compared with it only after resolving.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def _ref_file(root: Path, folder: Folder, ref_path: str) -> Path | None:
    """The file a ref's path names in this checkout, as the editor sees it.

    ``None`` when the file is gone, or when the path leaves the checkout: a
    ref is teammate-written, and must not point the editor at any other file
    on this machine. Under the folder, the path is spelled through the folder
    the editor opened (symlink and all), so it matches the open document.
    """
    file = (root / ref_path).resolve()
    if not file.is_relative_to(root) or not file.is_file():
        return None
    real = folder.path.resolve()
    return folder.path / file.relative_to(real) if file.is_relative_to(real) else file


def _split_ref(ref: str) -> tuple[str, str, str] | None:
    """``repo#path::Name`` as its three parts, or ``None`` for any other shape."""
    repo, _, rest = ref.partition("#")
    path, _, name = rest.rpartition("::")
    return (normalise(repo), path, name) if repo and path and name else None


@contextmanager
def _tool_calls() -> Iterator[None]:
    """Run tool calls with prompts off, and a guard refusal as an error.

    The local-store guard refuses a write by exiting, after printing why to
    stderr, which an editor shows nobody. Re-raised as an ordinary error, the
    explanation becomes the failed request's message.
    """
    from .cli.local_dispatch import LocalWriteRefused

    with prompts_off():
        try:
            yield
        except LocalWriteRefused as exc:
            raise RuntimeError(exc.message) from None


def _ui_bundle() -> dict[str, str]:
    """Where this install's web UI bundle is, for a webview to load.

    The installed bundle rather than a copy in each editor client: the page
    and the server it reads are released together, so the page always speaks
    to the tools it was built against.
    """
    from .ui_routes import BUNDLE_DIR, bundle_exists

    if not bundle_exists():
        raise RuntimeError(
            f"This witan install has no web UI bundle ({BUNDLE_DIR} has no "
            "index.html). A release from PyPI ships one; a source checkout "
            "builds it with `npm ci && npm run build` in mcp/servers/witan/ui."
        )
    return {"path": str(BUNDLE_DIR)}


def _required(args: Mapping[str, Any], key: str, command: str) -> Any:
    if not args.get(key):
        raise ValueError(f"{command} needs a {key!r} argument.")
    return args[key]


def _work_scope(args: Mapping[str, Any], command: str) -> tuple[str, str | None]:
    """A work list's ``(scope, project)``: the project scope when ``project``
    names one, else ``scope`` (default :data:`DEFAULT_SCOPE`)."""
    project = args.get("project")
    if project is not None:
        if not isinstance(project, str) or not project.startswith("wp-"):
            raise ValueError(f"{command}'s 'project' argument must be a wp- slug.")
        return SCOPE_PROJECT, project
    scope = args.get("scope") or DEFAULT_SCOPE
    if scope not in SCOPES:
        raise ValueError(
            f"{command}'s 'scope' argument must be one of {', '.join(SCOPES)}; "
            f"got {scope!r}."
        )
    return scope, None


def _task_scope(scope: str, project: str | None, repo: str | None) -> dict[str, Any]:
    """``task_ready``'s and ``task_list``'s arguments for a work-list scope."""
    if scope == SCOPE_PROJECT:
        return {"project_slug": project}
    if scope == SCOPE_ALL:
        return {"all_repos": True}
    if scope == SCOPE_REPO:
        # Sent only off its default: a deployment older than the parameter
        # rejects it (see `_scoped_tasks`).
        return {"repo": repo, "include_unfiled": False}
    return {"repo": repo}


def _scoped_tasks(
    call: Any, scope: str, project: str | None, repo: str | None, **kwargs: Any
) -> tuple[list[dict[str, Any]], str]:
    """``call`` (``task_ready`` or ``task_list``) in ``scope``, and the scope
    it was answered in.

    A deployment older than ``include_unfiled`` refuses the ``repo`` scope as
    an unexpected argument. That deployment's own default is ``repo+unfiled``,
    so the read is asked again in that scope and labelled as such, rather
    than failing every work list until the deployment is upgraded.
    """
    from witan_core.remote.proxy import RemoteToolFailed

    try:
        return call(**kwargs, **_task_scope(scope, project, repo)), scope
    except RemoteToolFailed as exc:
        if scope != SCOPE_REPO or "include_unfiled" not in str(exc):
            raise
    logger.warning(
        "witan.lsp.scope_fallback", reason="deployment predates include_unfiled"
    )
    fallback = SCOPE_REPO_UNFILED
    return call(**kwargs, **_task_scope(fallback, project, repo)), fallback


def _outside_stub(row: Mapping[str, Any]) -> dict[str, Any]:
    """An outside blocker's ``task_get`` row cut to ``OUTSIDE_BLOCKER_FIELDS``,
    plus ``lease_expired`` where the row has it."""
    stub = {field: row[field] for field in OUTSIDE_BLOCKER_FIELDS}
    if "lease_expired" in row:
        stub["lease_expired"] = row["lease_expired"]
    return stub


def _project_waves(ctx: Context, project: str) -> dict[str, Any]:
    """``project``'s tasks in dependency waves, from the reads the web UI's
    waves view makes: every task in the project, ``task_ready`` for it, and
    one ``task_get`` per blocker from outside it, kept only while open and
    cut to ``OUTSIDE_BLOCKER_FIELDS``."""
    tools = ctx.tools
    tasks = tools.task_list(project_slug=project)
    ready = tools.task_ready(project_slug=project, limit=MAX_TASK_LIMIT)
    inside = {t["slug"] for t in tasks}
    elsewhere = sorted(
        {
            blocker
            for t in tasks
            if t["status"] != "closed"
            for blocker in t["blocked_by"] or []
            if blocker not in inside
        }
    )
    outside = [
        _outside_stub(row)
        for slug in elsewhere
        if (row := tools.task_get(slug=slug)) is not None and row["status"] != "closed"
    ]
    plan = waves.layout(tasks, ready, outside)
    return {
        "waves": plan["waves"],
        "critical": plan["critical"],
        "cycles": plan["cycles"],
        "disagree": plan["disagree"],
        "tasks": plan["nodes"],
    }


class CouncilPlugin(Plugin):
    capabilities = frozenset({Capability.HOVER, Capability.DIAGNOSTICS})
    commands = (
        LIST_STALE_REFS,
        LIST_READY_TASKS,
        LIST_PROJECTS,
        LIST_MY_WORK,
        RUN_TASK,
        RUN_PROJECT,
        RELEASE_TASK,
        UI_BUNDLE,
        LIST_PROJECT_TASKS,
    )
    tools = frozenset(
        {
            "memory_stale_refs",
            "recall",
            "task_ready",
            "task_for_branch",
            "workflow_project_list",
            "task_list",
            "task_get",
            "workflow_project_get",
            "task_claim",
            "task_release",
            "workflow_project_run_memories",
        }
    ) | (
        # The web UI's reads, which the host relays for a webview (ADR 0012's
        # amendment on it). witan-code's are its own plugin's. Only those the
        # installed witan-core binds: one from before the relay refuses to
        # load a plugin that names a tool outside its binding.
        (READ_TOOLS - OPTIONAL_READ_TOOLS) & BOUND_TOOLS
    )

    def __init__(self) -> None:
        # Only the host's one worker thread touches these.
        self._stale_refs: dict[str, tuple[float, dict]] = {}
        self._roots: dict[Path, Path | None] = {}

    # ── routing ───────────────────────────────────────────────────────────

    def route(self, folder: Folder) -> Route:
        remote = cfg_module.load_remote_config(
            target=folder.target, project_path=folder.path
        )
        if remote is not None:
            return Route(key=remote, local=False)
        cfg = cfg_module.load(target=folder.target, project_path=folder.path)
        # The guard is per folder: a target that chose this store and a folder
        # that fell back to it read the same graph, but only the second's
        # writes are refused.
        guard = cfg_module.diagnose_local_dispatch(
            target=folder.target, project_path=folder.path
        )
        return Route(key=_store(cfg, folder), local=True, guard=guard)

    def open(self, route: Route, folder: Folder) -> Any:
        if not route.local:
            from .cli._common import remote_proxy

            return remote_proxy(route.key)
        from .cli.local_dispatch import local_server

        return local_server(route.guard)

    def tool_calls(self) -> AbstractContextManager[None]:
        return _tool_calls()

    # ── stale-ref diagnostics ─────────────────────────────────────────────

    def _root(self, folder: Folder) -> Path | None:
        if folder.path not in self._roots:
            self._roots[folder.path] = _checkout_root(folder.path)
        return self._roots[folder.path]

    def _stale(self, ctx: Context, *, fresh: bool = False) -> dict:
        cached = self._stale_refs.get(ctx.folder.uri)
        if (
            not fresh
            and cached
            and time.monotonic() - cached[0] < STALE_REFS_TTL_SECONDS
        ):
            return cached[1]
        result = ctx.tools.memory_stale_refs(repo=ctx.folder.repo)
        self._stale_refs[ctx.folder.uri] = (time.monotonic(), result)
        return result

    def diagnostics(self, ctx: Context, uri: str) -> list[Note]:
        folder = ctx.folder
        if folder.repo is None or (root := self._root(folder)) is None:
            return []
        path = _uri_path(uri).resolve()
        if not path.is_relative_to(root):
            return []
        relative = path.relative_to(root).as_posix()
        try:
            with _tool_calls():
                stale = self._stale(ctx)
        except Exception as exc:  # any read failure: a login, the network, the store
            # A request would fail with this message. A pushed diagnostic has
            # no request to fail, and raising would drop every other note the
            # host publishes for this file, so the reason goes on it instead.
            logger.warning("witan.lsp.stale_refs_failed", uri=uri, exc_info=True)
            return [Note(line=0, message=f"Could not check stale memory refs: {exc}")]
        notes = []
        for memory in stale["memories"]:
            for ref in memory["refs"]:
                parts = _split_ref(ref["symbol_ref"])
                if (
                    parts is None
                    or ref["status"] != "missing"
                    or parts[:2] != (folder.repo, relative)
                ):
                    continue
                candidates = ref.get("candidates") or []
                found = (
                    f"Candidates: {', '.join(candidates)}."
                    if candidates
                    else "No symbol of that name is indexed in the repo."
                )
                notes.append(
                    Note(
                        line=0,
                        message=(
                            f"{memory['kind']} {memory['slug']} "
                            f"({memory['title']}) names {parts[2]} in this file, "
                            f"which the code graph no longer has. {found}"
                        ),
                    )
                )
        return notes

    # ── hover ─────────────────────────────────────────────────────────────

    def hover(self, ctx: Context, uri: str, line: int, character: int) -> str | None:
        with _tool_calls():
            symbol = ctx.symbol_at(uri, line, character)
            if symbol is None:
                return None
            found = ctx.tools.recall(
                symbol_id=symbol,
                repo=symbol.partition("#")[0],
                hops=0,
                limit=HOVER_LIMIT,
            )
        tasks = found["symbol"]["tasks"][:HOVER_LIMIT]
        memories = found["memories"]
        if not tasks and not memories:
            return None
        lines = [f"**witan**: {escape_markdown(symbol.rpartition('::')[2])}"]
        if tasks:
            lines += ["", "Tasks:", ""]
            lines += [
                f"- {escape_markdown(t['slug'])} ({escape_markdown(t['status'])}): {escape_markdown(t['title'])}"
                for t in tasks
            ]
        if memories:
            lines += ["", "Memories:", ""]
            lines += [
                f"- {escape_markdown(m['slug'])} ({escape_markdown(m['kind'])}): {escape_markdown(m['title'])}"
                for m in memories
            ]
        return "\n".join(lines)

    # ── commands ──────────────────────────────────────────────────────────

    def execute_command(
        self, ctx: CommandContext, command: str, arguments: Sequence[Any]
    ) -> Any:
        args: Mapping[str, Any] = arguments[0] if arguments else {}
        if command == UI_BUNDLE:
            return _ui_bundle()
        if command == RELEASE_TASK:
            # No first-folder default: only the launch's own folder routes to
            # the graph the claim was taken in.
            _required(args, "uri", command)
        folder = self._folder(ctx, args.get("uri"))
        fctx = ctx.context(folder)
        if fctx is None:
            raise ValueError(
                f"witan serves no graph for {folder.path} in this language "
                "server; the diagnostics on any file in it say why."
            )
        with _tool_calls():
            if command in (RUN_TASK, RUN_PROJECT):
                return self._launch(ctx, fctx, command, args)
            if command == RELEASE_TASK:
                return fctx.tools.task_release(
                    slug=_required(args, "slug", command),
                    if_claimed_at=_required(args, "claimed_at", command),
                )
            # A project names no repo, so it lists without a remote.
            if command == LIST_PROJECT_TASKS:
                project = _required(args, "project", command)
                if not isinstance(project, str) or not project.startswith("wp-"):
                    raise ValueError(
                        f"{command}'s 'project' argument must be a wp- slug."
                    )
                return {
                    "repo": folder.repo,
                    "project": project,
                    **_project_waves(fctx, project),
                }
            scope, project = (
                _work_scope(args, command) if command in WORK_LISTS else (None, None)
            )
            # Every repo and one project name no repo, so a folder with a
            # graph but no remote can still list them.
            if folder.repo is None and scope not in (SCOPE_ALL, SCOPE_PROJECT):
                raise ValueError(
                    f"{folder.path} has no git remote, so there is no repo to "
                    f"scope {command} to."
                )
            if command in WORK_LISTS:
                # `_work_list` names the scope itself when it had to fall back.
                return {
                    "repo": folder.repo,
                    "scope": scope,
                    "project": project,
                    **self._work_list(fctx, command, scope, project),
                }
            if command == LIST_STALE_REFS:
                return {"repo": folder.repo, "refs": self._stale_ref_rows(fctx)}
            raise NotImplementedError(command)

    def _folder(self, ctx: CommandContext, uri: str | None) -> Folder:
        """The innermost folder holding ``uri`` (a folder or a file in one),
        or the first folder when the command names none."""
        if not ctx.folders:
            raise ValueError("No workspace folder is open.")
        if uri is None:
            return ctx.folders[0]
        # Resolved on both sides: a client may spell the uri through a
        # symlink the folder was not opened through, or the other way round.
        path = _uri_path(uri).resolve()
        real = {f.uri: f.path.resolve() for f in ctx.folders}
        containing = [f for f in ctx.folders if path.is_relative_to(real[f.uri])]
        if not containing:
            raise ValueError(f"{uri} is in no open workspace folder.")
        return max(containing, key=lambda f: len(real[f.uri].parts))

    def _work_list(
        self, ctx: Context, command: str, scope: str, project: str | None
    ) -> dict[str, Any]:
        tools, folder = ctx.tools, ctx.folder
        if command == LIST_READY_TASKS:
            # Read now: `folder.branch` is what was checked out when the
            # folder was added, and the person may have switched since.
            branch = checkout_branch(folder.path)
            ready, applied = _scoped_tasks(
                tools.task_ready, scope, project, folder.repo
            )
            return {
                "scope": applied,
                "branch": branch,
                "ready": ready,
                "for_branch": tools.task_for_branch(branch=branch, repo=folder.repo)
                if branch and folder.repo
                else [],
            }
        if command == LIST_PROJECTS:
            return {"projects": self._projects(ctx, scope, project)}
        # task_ready leaves out a task with a live claim, which is exactly the
        # person's own work in flight.
        tasks, applied = _scoped_tasks(
            tools.task_list,
            scope,
            project,
            folder.repo,
            status="in_progress",
            assignee="@me",
            **({"limit": MAX_TASK_LIMIT} if scope == SCOPE_ALL else {}),
        )
        return {"scope": applied, "tasks": tasks}

    def _projects(
        self, ctx: Context, scope: str, project: str | None
    ) -> list[dict[str, Any]]:
        tools, repo = ctx.tools, ctx.folder.repo
        if scope == SCOPE_PROJECT:
            # An unknown slug lists nothing, as task_ready and task_list do.
            row = tools.workflow_project_get(slug=project)
            return [] if row is None else [row]
        if scope == SCOPE_REPO:
            return tools.workflow_project_list(repo=repo)
        rows = tools.workflow_project_list(all_repos=True)
        if scope == SCOPE_ALL:
            return rows
        # workflow_project_list has no unfiled half and no limit to fill, so
        # filtering its all-repos read here is exact. A project filed under no
        # repo has an empty repo set.
        return [r for r in rows if not r["repos"] or repo in r["repos"]]

    def _stale_ref_rows(self, ctx: Context) -> list[dict[str, Any]]:
        """Every ref of the folder's repo that is not live, one row each.

        ``uri`` is the file the ref names in this checkout, or ``None`` when
        that file is gone (no document to put a diagnostic on) or the ref
        names another repo.
        """
        root = self._root(ctx.folder)
        rows = []
        for memory in self._stale(ctx, fresh=True)["memories"]:
            for ref in memory["refs"]:
                if ref["status"] == "live":
                    continue
                parts = _split_ref(ref["symbol_ref"])
                file = (
                    _ref_file(root, ctx.folder, parts[1])
                    if root is not None and parts and parts[0] == ctx.folder.repo
                    else None
                )
                rows.append(
                    {
                        "memory": memory["slug"],
                        "kind": memory["kind"],
                        "title": memory["title"],
                        "symbol_ref": ref["symbol_ref"],
                        "status": ref["status"],
                        "reason": ref.get("reason"),
                        "candidates": ref.get("candidates") or [],
                        "uri": file.as_uri() if file else None,
                    }
                )
        return rows

    def _launch(
        self,
        ctx: CommandContext,
        fctx: Context,
        command: str,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        """The launch spec for a task or project, in-process (ADR 0012, "3. The
        launch contract"). The client runs it; this process never spawns it."""
        from .cli.run_helpers import build_launch_spec

        folder = fctx.folder
        claim = args.get("claim", True)
        if not isinstance(claim, bool):
            raise TypeError(f"{command}'s 'claim' argument must be true or false.")
        spec = build_launch_spec(
            "task" if command == RUN_TASK else "project",
            _required(args, "slug", command),
            server=fctx.tools,
            cfg=cfg_module.load(target=folder.target, project_path=folder.path),
            workspace_folders=[
                folder.path,
                *(f.path for f in ctx.folders if f != folder),
            ],
            # `witan lsp --target` reaches this process as WITAN_TARGET.
            started_with_target="WITAN_TARGET" in os.environ,
            # `serve()` drops CLAUDE_PROJECT_DIR and WITAN_REPO before any
            # plugin runs, and a chat-panel agent inherits the editor's
            # environment, not this one.
            editor_environ=ctx.editor_environ,
            agent=args.get("agent"),
            model=args.get("model"),
            claim=claim,
        )
        if spec["launchable"] and spec["claim"]:
            # The folder the claim was routed through, which `cwd` need not be.
            spec["release"] = {
                "slug": spec["slug"],
                "claimed_at": spec["claim"]["claimed_at"],
                "uri": folder.uri,
            }
        return spec
