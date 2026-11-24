"""witan-code's language-server plugin and the `witan-code lsp` entrypoint.

The feature tests run the real plugin against a seeded local code graph,
through the host's JSON-RPC harness: three indexed checkouts on one store (an
app, the infra repo that provides one of its env vars, and a library it calls
into), an indexed feature branch of the app, an unindexed branch, and a repo
with no index at all.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from lsprotocol import types
from testsupport.sample_repos import COMPUTE, PULUMI, SERVICE, SETTINGS

from witan_code import config as cfg_module
from witan_code.lsp_plugin import (
    CALLERS_COMMAND,
    IMPACT_COMMAND,
    CodePlugin,
    _word_at,
)
from witan_core.lsp import Capability, Folder, escape_markdown
from witan_core.lsp.folders import resolve_folder
from witan_core.lsp.host import Host
from witan_core.lsp.plugin import discover_plugins
from witan_core.lsp.testing import lsp_session, spawned_status

from .conftest import git, git_checkout, requires_stack

RA = "https://github.com/test/app"
RI = "https://github.com/test/infra"
RL = "https://github.com/test/lib"

ADDED = SERVICE + "\n\ndef added():\n    return helper()\n"


def _checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _repo(path: Path, remote: str, files: dict[str, str]) -> Path:
    return git_checkout(path, remote=remote, files=files)


@dataclass(frozen=True)
class Seed:
    code_dir: Path
    app: Path
    lib: Path
    feature: Path
    unindexed: Path
    stray: Path


@pytest.fixture(scope="module")
def seed(tmp_path_factory) -> Seed:
    from witan_code import indexer

    root = tmp_path_factory.mktemp("graph")
    code_dir = root / "code"
    app = _repo(
        root / "app", RA, {"app/service.py": SERVICE, "app/settings.py": SETTINGS}
    )
    infra = _repo(root / "infra", RI, {"Pulumi.QA.yaml": PULUMI})
    lib = _repo(root / "lib", RL, {"lib/maths.py": COMPUTE})
    feature = root / "app-feature"
    git(app, "worktree", "add", "-q", "-b", "feature", str(feature))
    (feature / "app" / "service.py").write_text(ADDED)
    git(feature, "commit", "-qam", "added")
    unindexed = root / "app-unindexed"
    git(app, "worktree", "add", "-q", "-b", "unindexed", str(unindexed))
    stray = _repo(root / "stray", "https://github.com/test/stray", {"m.py": "x = 1\n"})
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("WITAN_CODE_DIR", str(code_dir))
        cfg = cfg_module.load()
        for path, repo in ((app, RA), (infra, RI), (lib, RL), (feature, RA)):
            indexer.index_path(path, repo_override=repo, config=cfg)
    return Seed(code_dir, app, lib, feature, unindexed, stray)


@pytest.fixture
def graph(seed, monkeypatch) -> Seed:
    """The seeded store, with the in-process server put back afterwards: the
    plugin points it at the store and detaches it from the cwd."""
    from witan_code import server as server_module

    monkeypatch.setenv("WITAN_CODE_DIR", str(seed.code_dir))
    monkeypatch.chdir(seed.app)
    before = server_module.cfg
    yield seed
    server_module.use_config(before)


def _host() -> Host:
    return Host({"code": CodePlugin()})


def _lens_client() -> types.ClientCapabilities:
    return types.ClientCapabilities(
        text_document=types.TextDocumentClientCapabilities(
            code_lens=types.CodeLensClientCapabilities()
        )
    )


def _doc(path: Path) -> types.TextDocumentIdentifier:
    return types.TextDocumentIdentifier(uri=path.as_uri())


def _at(line: int, character: int = 0) -> types.Position:
    return types.Position(line=line, character=character)


async def _hover(session, path: Path, line: int, character: int = 0) -> str | None:
    hover = await session.client.text_document_hover_async(
        types.HoverParams(text_document=_doc(path), position=_at(line, character))
    )
    return hover.contents.value if hover else None


def run(coro):
    return asyncio.run(coro)


# ── routing and startup ───────────────────────────────────────────────────


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """A deployed target and a local one, each matching a checkout by path."""
    deployed = _checkout(tmp_path / "work" / "app")
    local = _checkout(tmp_path / "personal" / "dots")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[targets.work]\n"
        'remote_url = "https://witan.example.org/mcp"\n'
        'oidc_issuer = "https://sso.example.org/realms/ol"\n'
        f'match_paths = ["{deployed.parent}"]\n'
        "[targets.personal]\n"
        f'code_dir = "{tmp_path / "personal-code"}"\n'
        f'match_paths = ["{local.parent}"]\n'
    )
    monkeypatch.setenv("WITAN_CONFIG", str(cfg))
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    return deployed, local


def _folder(path: Path, target: str | None) -> Folder:
    return Folder(uri=path.as_uri(), path=path, repo=None, branch=None, target=target)


def test_only_the_code_plugin_is_installed_here():
    plugins = discover_plugins()
    assert list(plugins) == ["code"]
    assert isinstance(plugins["code"], CodePlugin)
    assert Capability.SYMBOL_RESOLUTION in plugins["code"].capabilities


def test_witan_code_lsp_starts_and_reports_one_plugin(folders):
    deployed, local = folders
    init, status = asyncio.run(
        spawned_status(
            [sys.executable, "-c", "from witan_code.cli import cli; cli()", "lsp"],
            [deployed, local],
        )
    )
    assert init.server_info.name == "witan"
    assert init.capabilities.hover_provider
    assert init.capabilities.code_lens_provider.resolve_provider
    assert set(init.capabilities.execute_command_provider.commands) >= {
        CALLERS_COMMAND,
        IMPACT_COMMAND,
    }
    assert [p["name"] for p in status["plugins"]] == ["code"]
    assert [f["target"] for f in status["folders"]] == ["work", "personal"]


def test_routes_follow_each_folders_target(folders, tmp_path, monkeypatch):
    deployed, local = folders
    monkeypatch.chdir(deployed)
    plugin = CodePlugin()

    remote = plugin.route(_folder(deployed, "work"))
    assert not remote.local
    assert remote.key.url == "https://witan.example.org/mcp"
    personal = plugin.route(_folder(local, "personal"))
    assert personal.local
    assert personal.key == (str(tmp_path / "personal-code"), None)
    stray = _checkout(tmp_path / "stray")
    assert plugin.route(_folder(stray, None)).key == (
        str(cfg_module.load(project_path=stray).code_dir),
        None,
    )
    assert cfg_module.load_remote_config(project_path=stray) is None


def test_a_local_folder_points_the_server_at_its_own_stores(folders, tmp_path):
    from witan_code import server as server_module

    _, local = folders
    plugin = CodePlugin()
    folder = _folder(local, "personal")
    before = server_module.cfg
    try:
        assert plugin.open(plugin.route(folder), folder) is server_module
        assert server_module.cfg.code_dir == tmp_path / "personal-code"
        # One process serves every folder, so the cwd's checkout is no one's.
        assert server_module._cached_detect() is None
    finally:
        server_module.use_config(before)


def test_a_deployed_folder_opens_a_remote_proxy(folders):
    from witan_code.remote.proxy import RemoteServerProxy

    deployed, _ = folders
    plugin = CodePlugin()
    folder = _folder(deployed, "work")
    assert isinstance(plugin.open(plugin.route(folder), folder), RemoteServerProxy)


def test_a_relative_code_dir_resolves_against_each_folder(tmp_path, monkeypatch):
    from witan_code import server as server_module

    one = _checkout(tmp_path / "one")
    two = _checkout(tmp_path / "two")
    cfg = tmp_path / "config.toml"
    cfg.write_text('code_dir = ".witan-code"\n')
    monkeypatch.setenv("WITAN_CONFIG", str(cfg))
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.chdir(tmp_path)
    plugin = CodePlugin()
    routes = [plugin.route(_folder(p, None)) for p in (one, two)]
    assert routes[0].key != routes[1].key
    before = server_module.cfg
    try:
        plugin.open(routes[1], _folder(two, None))
        assert server_module.cfg.code_dir == two / ".witan-code"
    finally:
        server_module.use_config(before)


def test_a_deployment_without_a_login_says_so_and_never_prompts(tmp_path, monkeypatch):
    """No `witan login` token: the token provider raises rather than prompts,
    so nothing reads stdin (the JSON-RPC channel), even when it is a terminal.
    The merged features show the reason; definition fails with it."""
    from witan_core.remote.oidc import NeedsLogin

    app = _repo(tmp_path / "work" / "app", RA, {"app/service.py": SERVICE})
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[targets.work]\n"
        'remote_url = "https://witan.example.org/mcp"\n'
        'oidc_issuer = "https://sso.example.org/realms/ol"\n'
        f'match_paths = ["{app.parent}"]\n'
    )
    monkeypatch.setenv("WITAN_CONFIG", str(cfg))
    monkeypatch.delenv("WITAN_CODE_DIR", raising=False)

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    def no_prompt(*_args):
        raise AssertionError("prompted for input")

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr("builtins.input", no_prompt)
    host = _host()
    host.set_folders([resolve_folder(app.as_uri(), app, None)])
    path = app / "app" / "service.py"
    uri = path.as_uri()
    try:
        hover = host.hover(path, uri, 9, 12)
        assert hover.startswith(
            "witan-code: " + escape_markdown("Not logged in to https://witan")
        )
        assert escape_markdown("`witan-code login`") in hover
        [note] = host.diagnostics(path, uri)
        assert note.message.startswith("code features unavailable: Not logged in")
        assert host.code_lenses(path, uri) == []
        with pytest.raises(NeedsLogin, match="witan-code login"):
            host.definition(path, uri, 9, 12)
    finally:
        host.close()


# ── features against the seeded graph ─────────────────────────────────────


@requires_stack
def test_symbol_resolution_names_the_innermost_symbol(graph):
    host = _host()
    host.set_folders([resolve_folder(graph.app.as_uri(), graph.app, None)])
    folder = host.folders[graph.app.as_uri()]
    uri = (graph.app / "app" / "service.py").as_uri()
    try:
        assert host.resolve_symbol(folder, uri, 5, 8) == (
            f"{RA}#app/service.py::Service.run"
        )
        assert host.resolve_symbol(folder, uri, 3, 0) == f"{RA}#app/service.py::Service"
        assert (
            host.resolve_symbol(folder, uri, 1, 0) == f"{RA}#app/service.py::<module>"
        )
    finally:
        host.close()


@requires_stack
def test_lenses_resolve_to_callers_and_cross_repo_impact(graph):
    service = graph.app / "app" / "service.py"
    settings = graph.app / "app" / "settings.py"

    async def go():
        async with lsp_session(
            _host(), [graph.app], client_capabilities=_lens_client()
        ) as session:
            lenses = await session.client.text_document_code_lens_async(
                types.CodeLensParams(text_document=_doc(service))
            )
            assert all(lens.command is None for lens in lenses)
            by_line = {lens.range.start.line: lens for lens in lenses}
            assert {3, 4, 8, 12} <= set(by_line)
            helper = await session.client.code_lens_resolve_async(by_line[8])
            assert helper.command.title == "1 caller"
            assert helper.command.command == CALLERS_COMMAND
            callers = await session.command(CALLERS_COMMAND, *helper.command.arguments)
            assert callers == [
                {
                    "title": "Service.run",
                    "repo": RA,
                    "path": "app/service.py",
                    "line": 5,
                    "uri": service.as_uri(),
                }
            ]

            lenses = await session.client.text_document_code_lens_async(
                types.CodeLensParams(text_document=_doc(settings))
            )
            callers, impact = [
                await session.client.code_lens_resolve_async(lens) for lens in lenses
            ]
            assert callers.command.title == "0 callers"
            assert impact.range.start.line == 3
            assert impact.command.title == "1 cross-repo binding in 1 repo"
            bindings = await session.command(IMPACT_COMMAND, *impact.command.arguments)
            assert [(b["repo"], b["path"], b["uri"]) for b in bindings] == [
                (RI, "Pulumi.QA.yaml", None)
            ]
            assert bindings[0]["title"] == "provider of env_var MITOL_APP_BASE_URL"

    run(go())


@requires_stack
def test_without_codelens_the_counts_are_in_hover_and_a_code_action(graph):
    """eglot's view: no CodeLens, so the same text arrives in hover and the
    command as a code action."""
    service = graph.app / "app" / "service.py"

    async def go():
        async with lsp_session(_host(), [graph.app]) as session:
            assert "1 caller" in await _hover(session, service, 8)
            actions = await session.client.text_document_code_action_async(
                types.CodeActionParams(
                    text_document=_doc(service),
                    range=types.Range(start=_at(8), end=_at(8)),
                    context=types.CodeActionContext(diagnostics=[]),
                )
            )
            assert [(a.title, a.command) for a in actions] == [
                ("1 caller", CALLERS_COMMAND)
            ]

    run(go())


@requires_stack
def test_hover_on_an_env_var_lists_providers_and_consumers(graph):
    settings = graph.app / "app" / "settings.py"

    async def go():
        async with lsp_session(_host(), [graph.app]) as session:
            text = await _hover(session, settings, 4, 30)
            assert text.startswith("**env_var** MITOL_APP_BASE_URL")
            providers, consumers = text.split("Consumers:")
            # infra is not checked out here, so it is named, not linked.
            assert (
                f"- {escape_markdown('https://github.com/test/infra Pulumi.QA.yaml')}\n"
                in providers
            )
            assert f"]({settings.as_uri()}#L5)" in consumers
            assert await _hover(session, settings, 0) is None

    run(go())


@requires_stack
def test_definition_crosses_repos_and_names_what_is_not_checked_out(graph):
    service = graph.app / "app" / "service.py"
    compute = graph.lib / "lib" / "maths.py"

    async def definition(session):
        return await session.client.text_document_definition_async(
            types.DefinitionParams(text_document=_doc(service), position=_at(9, 12))
        )

    async def go():
        async with lsp_session(_host(), [graph.app, graph.lib]) as session:
            [loc] = await definition(session)
            assert (loc.uri, loc.range.start.line) == (compute.as_uri(), 0)
            assert await _hover(session, service, 9, 12) is None
        async with lsp_session(_host(), [graph.app]) as session:
            assert await definition(session) == []
            text = await _hover(session, service, 9, 12)
            assert "Defined in a repo not open here" in text
            assert (
                escape_markdown("https://github.com/test/lib lib/maths.py:1 compute")
                in text
            )
            assert "Referenced from" in text
            assert escape_markdown("lib/maths.py:5 twice") in text

    run(go())


@requires_stack
def test_references_read_the_folders_own_branch(graph):
    """The feature branch adds a caller of `helper`. Its folder sees it; the
    main checkout's folder, open alongside, does not."""

    async def references(session, root):
        locs = await session.client.text_document_references_async(
            types.ReferenceParams(
                text_document=_doc(root / "app" / "service.py"),
                position=_at(8, 5),
                context=types.ReferenceContext(include_declaration=False),
            )
        )
        return sorted(loc.range.start.line for loc in locs)

    async def go():
        async with lsp_session(_host(), [graph.app, graph.feature]) as session:
            assert await references(session, graph.app) == [4]
            assert await references(session, graph.feature) == [4, 17]

    run(go())


@requires_stack
def test_diagnostics_name_a_missing_index(graph):
    async def go():
        async with lsp_session(
            _host(), [graph.app, graph.feature, graph.unindexed, graph.stray]
        ) as session:
            assert await session.open(graph.app / "app" / "service.py") == []
            assert await session.open(graph.feature / "app" / "service.py") == []
            [branch] = await session.open(graph.unindexed / "app" / "service.py")
            assert branch.range.start.line == 0
            assert branch.severity == types.DiagnosticSeverity.Information
            assert "branch unindexed has no code index" in branch.message
            [repo] = await session.open(graph.stray / "m.py")
            assert "https://github.com/test/stray has no code index" in repo.message

    run(go())


@requires_stack
def test_a_branch_switch_reaches_the_next_read(graph, tmp_path, monkeypatch):
    """The editor reports a folder's branch once; a `git switch` after that
    must still change which view the reads use."""
    from witan_code import lsp_plugin

    monkeypatch.setattr(lsp_plugin, "_BRANCH_TTL_SECONDS", 0.0)
    checkout = tmp_path / "switching"
    git(
        graph.app,
        "worktree",
        "add",
        "-q",
        "-b",
        f"scratch-{tmp_path.name}",
        str(checkout),
    )
    host = _host()
    host.set_folders([resolve_folder(checkout.as_uri(), checkout, None)])
    path = checkout / "app" / "service.py"
    try:
        [note] = host.diagnostics(path, path.as_uri())
        assert f"branch scratch-{tmp_path.name} has no code index" in note.message
        git(checkout, "switch", "-q", "--detach")
        assert host.diagnostics(path, path.as_uri()) == []
    finally:
        host.close()
        git(graph.app, "worktree", "remove", "--force", str(checkout))


@requires_stack
def test_the_default_branch_reads_main_beside_a_branch_named__main(graph, tmp_path):
    """A git branch named `_main` indexes onto the `_main` view, which is also
    where an explicit read of branch `main` maps. A folder on the default
    branch must read the main view instead."""
    from witan_code import indexer
    from witan_code import server as server_module

    repo = "https://github.com/test/solo"
    solo = _repo(tmp_path / "solo", repo, {"m.py": "def shared():\n    return 1\n"})
    under = tmp_path / "solo-under"
    git(solo, "worktree", "add", "-q", "-b", "_main", str(under))
    (under / "m.py").write_text(
        "def shared():\n    return 1\n\n\ndef only_on_underscore_main():\n    return 2\n"
    )
    git(under, "commit", "-qam", "more")
    for path in (solo, under):
        indexer.index_path(path, repo_override=repo, config=cfg_module.load())

    host = _host()
    host.set_folders([resolve_folder(solo.as_uri(), solo, None)])
    path = solo / "m.py"
    try:
        lenses = host.code_lenses(path, path.as_uri())
        assert [lens.data["data"]["symbol"] for lens in lenses] == [
            f"{repo}#m.py::shared"
        ]
        # The collision being avoided: naming `main` reads the `_main` view.
        named = asyncio.run(
            server_module.code_symbols_in_file(path="m.py", repo=repo, branch="main")
        )
        assert "only_on_underscore_main" in {s["name"] for s in named}
    finally:
        host.close()


@requires_stack
def test_definition_reads_an_open_dependency_at_its_branch(graph, tmp_path):
    """The library is open on a branch that adds `fresh`; main has no such
    function, so a fallback that read main everywhere would find nothing."""
    from witan_code import indexer

    lib_branch = tmp_path / "lib-fresh"
    git(
        graph.lib,
        "worktree",
        "add",
        "-q",
        "-b",
        f"fresh-{tmp_path.name}",
        str(lib_branch),
    )
    maths = lib_branch / "lib" / "maths.py"
    maths.write_text(COMPUTE + "\n\ndef fresh():\n    return 3\n")
    git(lib_branch, "commit", "-qam", "fresh")
    indexer.index_path(lib_branch, repo_override=RL, config=cfg_module.load())
    service = graph.app / "app" / "service.py"

    async def go():
        async with lsp_session(_host(), [graph.app, lib_branch]) as session:
            await session.open(service, "x = fresh()\n")
            return await session.client.text_document_definition_async(
                types.DefinitionParams(text_document=_doc(service), position=_at(0, 6))
            )

    try:
        [loc] = run(go())
        assert (loc.uri, loc.range.start.line) == (maths.as_uri(), 8)
    finally:
        git(graph.lib, "worktree", "remove", "--force", str(lib_branch))


def test_the_identifier_under_the_cursor_includes_its_end():
    """A cursor just after a word (the column `match.end()` names) still
    means that word, as editors place it after typing one."""
    line = "x = fresh()"
    assert [_word_at(line, 0, column) for column in (4, 6, 9, 10)] == [
        "fresh",
        "fresh",
        "fresh",
        None,
    ]


def test_a_whole_file_function_wins_over_its_module(monkeypatch):
    """Both span every line; the module is only the fallback."""
    plugin = CodePlugin()
    rows = [
        {
            "symbol_id": "r#w.py::<module>",
            "kind": "module",
            "start_line": 1,
            "end_line": 2,
        },
        {
            "symbol_id": "r#w.py::whole",
            "kind": "function",
            "start_line": 1,
            "end_line": 2,
        },
    ]
    monkeypatch.setattr(plugin, "_symbols", lambda ctx, uri: rows)
    assert plugin.resolve_symbol(None, "file:///w.py", 1, 0) == "r#w.py::whole"


@requires_stack
def test_an_absolute_path_reads_its_own_checkout_not_the_cwds(graph):
    """Detached from the cwd, the server relativises an absolute path against
    the checkout that holds it; the cwd here is the app, not the library."""
    from witan_code import server as server_module

    server_module.use_config(cfg_module.load(), follow_checkout=False)
    rows = asyncio.run(
        server_module.code_symbols_in_file(
            path=str(graph.lib / "lib" / "maths.py"), repo=RL
        )
    )
    assert {"compute", "twice"} <= {r["name"] for r in rows}
