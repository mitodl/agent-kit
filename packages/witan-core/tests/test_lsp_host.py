"""The language-server host, driven over real JSON-RPC by the in-process harness."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from lsprotocol import types

from witan_core.lsp import BOUND_TOOLS, Capability, Lens, Note, Plugin, Route
from witan_core.lsp.folders import explicit_target, resolve_folder
from witan_core.lsp.host import Host, PluginExit
from witan_core.lsp.testing import lsp_session


def _checkout(path: Path, remote: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote], check=True
    )
    return path


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A config.toml with two targets, each matching one checkout by path."""
    work = _checkout(tmp_path / "work" / "app", "git@github.com:Work/App.git")
    home = _checkout(tmp_path / "home" / "dots", "https://github.com/me/dots")
    cfg = tmp_path / "config.toml"
    # json.dumps writes a valid TOML basic string. An f-string would not on
    # Windows, where a path's `\U` reads as a unicode escape.
    cfg.write_text(
        f"[targets.work]\nmatch_paths = [{json.dumps(str(work.parent))}]\n"
        f"[targets.personal]\nmatch_paths = [{json.dumps(str(home.parent))}]\n"
    )
    monkeypatch.setenv("WITAN_CONFIG", str(cfg))
    return work, home


class Recorder(Plugin):
    """A plugin whose routes and results the test controls."""

    capabilities = frozenset(
        {Capability.HOVER, Capability.CODE_LENS, Capability.DIAGNOSTICS}
    )
    tools = frozenset({"task_get"})

    def __init__(
        self,
        label: str,
        routes: dict[str | None, Route] | None = None,
        commands: tuple[str, ...] = (),
    ) -> None:
        self.label = label
        self.commands = commands
        self.routes = routes or {}
        self.opened: list[tuple[Route, Path]] = []

    def route(self, folder):
        return self.routes.get(folder.target, Route(key="shared", local=True))

    def open(self, route, folder):
        self.opened.append((route, folder.path))
        return self

    def hover(self, ctx, uri, line, character):
        return f"{self.label} on {ctx.folder.target}"

    def code_lenses(self, ctx, uri):
        return [Lens(line=1, title=self.label, command="witan.echo")]

    def diagnostics(self, ctx, uri):
        return [Note(line=2, message=f"{self.label} note")]

    def execute_command(self, ctx, command, arguments):
        return {"plugin": self.label, "arguments": list(arguments)}


def run(coro):
    return asyncio.run(coro)


def test_empty_host_advertises_only_its_own_command(config):
    work, _ = config

    async def go():
        async with lsp_session(Host({}), [work]) as session:
            caps = session.capabilities
            assert caps.hover_provider is None
            assert caps.code_lens_provider is None
            assert caps.definition_provider is None
            assert list(caps.execute_command_provider.commands) == ["witan.status"]
            assert (await session.status())["plugins"] == []
            assert session.initialize_result.server_info.name == "witan"

    run(go())


def test_two_folders_resolve_to_their_own_targets(config):
    work, home = config

    async def go():
        async with lsp_session(Host({}), [work, home]) as session:
            folders = (await session.status())["folders"]
            assert [(f["target"], f["repo"], f["branch"]) for f in folders] == [
                ("work", "https://github.com/work/app", "main"),
                ("personal", "https://github.com/me/dots", "main"),
            ]
            assert "work" in session.log_messages[0]
            assert "personal" in session.log_messages[0]

    run(go())


def test_explicit_target_wins_over_matching(config, monkeypatch):
    work, home = config
    monkeypatch.setenv("WITAN_TARGET", "personal")

    async def go():
        async with lsp_session(
            Host({}), [work, home], target=explicit_target(None)
        ) as session:
            folders = (await session.status())["folders"]
            assert [f["target"] for f in folders] == ["personal", "personal"]

    run(go())


def test_unknown_explicit_target_is_refused(config):
    with pytest.raises(ValueError, match="Unknown target 'nope'"):
        explicit_target("nope")


def test_folder_resolution_ignores_the_process_cwd(config, monkeypatch, tmp_path):
    work, _ = config
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WITAN_REPO", "https://github.com/elsewhere/other")
    folder = resolve_folder(work.as_uri(), work, None)
    assert (folder.target, folder.repo) == ("work", "https://github.com/work/app")


def test_plugins_merge_in_entry_point_order(config, monkeypatch):
    work, _ = config
    monkeypatch.chdir(work)
    host = Host({"a": Recorder("first"), "b": Recorder("second")})

    async def go():
        async with lsp_session(host, [work]) as session:
            caps = session.capabilities
            assert caps.hover_provider
            assert caps.code_lens_provider is not None
            uri = (work / "m.py").as_uri()
            notes = await session.open(work / "m.py")
            assert [d.message for d in notes] == ["first note", "second note"]
            assert all(
                d.severity == types.DiagnosticSeverity.Information for d in notes
            )
            hover = await session.client.text_document_hover_async(
                types.HoverParams(
                    text_document=types.TextDocumentIdentifier(uri=uri),
                    position=types.Position(line=0, character=0),
                )
            )
            assert hover.contents.value == "first on work\n\n---\n\nsecond on work"
            lenses = await session.client.text_document_code_lens_async(
                types.CodeLensParams(
                    text_document=types.TextDocumentIdentifier(uri=uri)
                )
            )
            assert [lens.command.title for lens in lenses] == ["first", "second"]

    run(go())


def test_a_command_has_one_owner():
    echo = ("witan.echo",)
    with pytest.raises(ValueError, match="registered twice"):
        Host({"a": Recorder("1", commands=echo), "b": Recorder("2", commands=echo)})
    with pytest.raises(ValueError, match="registered twice"):
        Host({"a": Recorder("1", commands=("witan.status",))})


def test_commands_are_advertised_and_dispatched(config, monkeypatch):
    work, _ = config
    monkeypatch.chdir(work)

    async def go():
        host = Host({"a": Recorder("one", commands=("witan.echo",))})
        async with lsp_session(host, [work]) as session:
            assert sorted(session.capabilities.execute_command_provider.commands) == [
                "witan.echo",
                "witan.status",
            ]
            assert await session.command("witan.echo", 1, "x") == {
                "plugin": "one",
                "arguments": [1, "x"],
            }

    run(go())


def test_one_provider_per_remote_target_and_one_local_store(config, monkeypatch):
    work, home = config
    monkeypatch.chdir(home.parent)
    plugin = Recorder(
        "p",
        routes={
            "work": Route(key="https://witan.example/mcp", local=False),
            "personal": Route(key="/stores/personal", local=True),
            None: Route(key="/stores/default", local=True),
        },
    )
    other = _checkout(work.parent / "other", "https://github.com/work/other")
    stray = _checkout(home.parent.parent / "stray", "https://github.com/me/stray")
    host = Host({"p": plugin})

    async def go():
        async with lsp_session(host, [work, home, other, stray]) as session:
            assert await session.open(work / "a.py") == await session.open(
                other / "b.py"
            )
            assert [m.message for m in await session.open(home / "c.py")] == ["p note"]
            refused = await session.open(stray / "d.py")
            assert len(refused) == 1
            assert "/stores/default" in refused[0].message
            assert "/stores/personal" in refused[0].message

    run(go())
    assert plugin.opened == [
        (Route(key="https://witan.example/mcp", local=False), work),
        (Route(key="/stores/personal", local=True), home),
    ]
    # The servers configure their local store from the cwd at import.
    assert Path.cwd() == home


def test_plugin_exit_fails_the_call_not_the_server():
    class Exits(Recorder):
        def hover(self, ctx, uri, line, character):
            raise SystemExit(1)

    plugin = Exits("x")
    host = Host({"x": plugin})

    async def go():
        with pytest.raises(PluginExit):
            await host.run(plugin.hover, None, "", 0, 0)
        return await host.run(host.status)

    assert run(go())["plugins"][0]["name"] == "x"
    host.close()


def test_tools_outside_the_binding_are_refused():
    class Greedy(Plugin):
        tools = frozenset({"task_close"})

    with pytest.raises(ValueError, match="task_close"):
        Host({"g": Greedy()})


def test_commands_must_be_namespaced():
    class Loose(Plugin):
        commands = ("listTasks",)

    with pytest.raises(ValueError, match="witan"):
        Host({"l": Loose()})


def test_bound_tools_expose_only_declared_tools(config, monkeypatch):
    work, _ = config
    monkeypatch.chdir(work)

    class Provider:
        def task_get(self, slug):
            return {"slug": slug}

        async def task_list(self):
            return []

    class Uses(Recorder):
        tools = frozenset({"task_get"})

        def open(self, route, folder):
            return Provider()

    host = Host({"u": Uses("u")})
    folder = resolve_folder(work.as_uri(), work, None)
    host.set_folders([folder])
    ctx = host.context("u", folder)
    assert ctx.tools.task_get(slug="tk-1") == {"slug": "tk-1"}
    # RemoteServerProxy refuses positional arguments, so a plugin that passed
    # them would work against a local store and fail against a deployment.
    with pytest.raises(TypeError, match="keyword"):
        ctx.tools.task_get("tk-1")
    with pytest.raises(AttributeError, match="task_list"):
        ctx.tools.task_list  # noqa: B018
    host.close()


def test_symbol_resolution_comes_from_the_plugin_that_provides_it(config, monkeypatch):
    work, _ = config
    monkeypatch.chdir(work)

    class Symbols(Recorder):
        capabilities = frozenset({Capability.SYMBOL_RESOLUTION})

        def resolve_symbol(self, ctx, uri, line, character):
            return f"{ctx.folder.repo}#m.py::f@{line}"

    class Asks(Recorder):
        capabilities = frozenset({Capability.HOVER})

        def hover(self, ctx, uri, line, character):
            return ctx.symbol_at(uri, line, character) or "no resolver"

    folder = resolve_folder(work.as_uri(), work, None)
    for plugins, expected in (
        ({"a": Asks("a"), "s": Symbols("s")}, "https://github.com/work/app#m.py::f@3"),
        ({"a": Asks("a")}, "no resolver"),
    ):
        host = Host(plugins)
        host.set_folders([folder])
        assert host.hover(work / "m.py", "", 3, 0) == expected
        host.close()


def test_bound_tools_list_matches_the_adr():
    adr = (
        Path(__file__).resolve().parents[3]
        / "mcp/servers/witan/docs/adr/0012-editor-integration-architecture.md"
    ).read_text()
    for tool in BOUND_TOOLS:
        assert f"`{tool}" in adr, tool


def test_the_plugin_interface_imports_without_the_lsp_extra():
    """witan-council and witan-code import it at module scope, and
    check-core-floor imports them without pygls installed."""
    import sys

    probe = (
        "import sys, witan_core.lsp, witan_core.lsp.host, witan_core.lsp.folders;"
        "assert not {'pygls', 'lsprotocol'} & set(sys.modules), sorted(sys.modules)"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def test_the_first_listed_folder_anchors_the_local_store(config, monkeypatch):
    work, home = config
    monkeypatch.chdir(work)
    plugin = Recorder(
        "p",
        routes={
            "work": Route(key="/stores/work", local=True),
            "personal": Route(key="/stores/personal", local=True),
        },
    )

    async def go():
        async with lsp_session(Host({"p": plugin}), [work, home]) as session:
            # Opened first but listed second, so it is the refused one.
            refused = await session.open(home / "a.py")
            assert len(refused) == 1
            assert "/stores/personal, not /stores/work" in refused[0].message
            assert [n.message for n in await session.open(work / "b.py")] == ["p note"]

    run(go())


def test_a_folder_refused_by_two_plugins_gets_one_note(config, monkeypatch):
    work, home = config
    monkeypatch.chdir(work)
    routes = {
        "work": Route(key="/stores/work", local=True),
        "personal": Route(key="/stores/personal", local=True),
    }
    host = Host({"a": Recorder("a", routes), "b": Recorder("b", routes)})
    host.set_folders([resolve_folder(p.as_uri(), p, None) for p in (work, home)])
    notes = host.diagnostics(home / "x.py", (home / "x.py").as_uri())
    assert len(notes) == 1
    assert "no a, b features" in notes[0].message
    host.close()


def test_a_non_file_folder_gets_nothing_and_breaks_nothing(config, monkeypatch):
    work, _ = config
    monkeypatch.chdir(work)
    virtual = types.WorkspaceFolder(uri="vscode-vfs://example/org/repo", name="vfs")

    async def go():
        async with lsp_session(Host({"a": Recorder("a")}), [work]) as session:
            session.client.workspace_did_change_workspace_folders(
                types.DidChangeWorkspaceFoldersParams(
                    event=types.WorkspaceFoldersChangeEvent(added=[virtual], removed=[])
                )
            )
            assert [n.message for n in await session.open(work / "m.py")] == ["a note"]
            folders = (await session.status())["folders"]
            assert [f["uri"] for f in folders] == [work.as_uri()]

    run(go())


def test_a_config_error_costs_one_folder_its_features(config, monkeypatch):
    work, home = config
    monkeypatch.chdir(home)

    class Misconfigured(Recorder):
        def route(self, folder):
            if folder.target == "work":
                raise ValueError("remote_url is set without oidc_issuer")
            return Route(key="/stores/personal", local=True)

    async def go():
        host = Host({"m": Misconfigured("m")})
        async with lsp_session(host, [work, home]) as session:
            reason = "remote_url is set without oidc_issuer"
            assert [n.message for n in await session.open(work / "a.py")] == [
                f"witan: no m features in this folder: {reason}"
            ]
            assert [n.message for n in await session.open(home / "b.py")] == ["m note"]

    run(go())
