"""The language-server host, driven over real JSON-RPC by the in-process harness."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from lsprotocol import types

from witan_core.lsp import (
    BOUND_TOOLS,
    Capability,
    Lens,
    Location,
    Note,
    Plugin,
    Route,
    escape_markdown,
)
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


def test_a_store_change_after_anchoring_refuses_rather_than_rerouting(
    config, monkeypatch
):
    """The anchor's store is already open. Re-resolving the anchor when a
    folder is added would compare against wherever its target points now,
    and accept a second store."""
    work, home = config
    monkeypatch.chdir(work)
    plugin = Recorder("p", {"work": Route(key="/stores/a", local=True)})
    host = Host({"p": plugin})
    host.set_folders([resolve_folder(work.as_uri(), work, None)])
    assert host.context("p", host.folders[work.as_uri()]) is not None
    plugin.routes = {
        "work": Route(key="/stores/b", local=True),
        "personal": Route(key="/stores/b", local=True),
    }
    host.change_folders([resolve_folder(home.as_uri(), home, None)], ())
    notes = host.diagnostics(home / "x.py", (home / "x.py").as_uri())
    assert len(notes) == 1
    assert "/stores/b, not /stores/a" in notes[0].message
    host.close()


def test_each_plugin_anchors_its_own_first_routable_folder(config, monkeypatch):
    """One plugin's config error in the first folder must not cost a healthy
    second folder that plugin's features."""
    work, home = config
    monkeypatch.chdir(work)
    routed: list[str | None] = []

    class BrokenAtWork(Recorder):
        def route(self, folder):
            routed.append(folder.target)
            if folder.target == "work":
                raise ValueError("bad code config")
            return Route(key="/stores/personal-code", local=True)

    host = Host(
        {
            "a": Recorder(
                "a",
                {
                    "work": Route(key="/stores/shared", local=True),
                    "personal": Route(key="/stores/shared", local=True),
                },
            ),
            "b": BrokenAtWork("b"),
        }
    )
    host.set_folders([resolve_folder(p.as_uri(), p, None) for p in (work, home)])
    notes = host.diagnostics(home / "x.py", (home / "x.py").as_uri())
    assert [n.message for n in notes] == ["a note", "b note"]
    # Each folder routed once per plugin: the anchor is not routed again.
    assert routed == ["work", "personal"]
    host.close()


def test_a_commands_only_plugin_anchors_in_folder_order_and_explains(
    config, monkeypatch
):
    work, home = config
    monkeypatch.chdir(work)

    class CommandsOnly(Recorder):
        capabilities = frozenset()

    plugin = CommandsOnly(
        "c",
        {
            "work": Route(key="/stores/work", local=True),
            "personal": Route(key="/stores/personal", local=True),
        },
        commands=("witan.echo",),
    )
    host = Host({"c": plugin})
    host.set_folders([resolve_folder(p.as_uri(), p, None) for p in (work, home)])
    # Asked about the second folder first, it is still the one refused.
    assert host.context("c", host.folders[home.as_uri()]) is None
    assert host.context("c", host.folders[work.as_uri()]) is not None
    notes = host.diagnostics(home / "x.py", (home / "x.py").as_uri())
    assert len(notes) == 1
    assert "no c features" in notes[0].message
    host.close()


def test_a_relative_witan_config_survives_pinning_the_cwd(
    config, monkeypatch, tmp_path
):
    work, home = config
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WITAN_CONFIG", "config.toml")
    host = Host({"p": Recorder("p")})
    host.set_folders([resolve_folder(work.as_uri(), work, None)])
    assert host.context("p", host.folders[work.as_uri()]) is not None
    assert Path.cwd() == work
    assert resolve_folder(home.as_uri(), home, None).target == "personal"
    host.close()


def test_references_pass_include_declaration_through(config, monkeypatch):
    work, _ = config
    monkeypatch.chdir(work)

    class Refs(Recorder):
        capabilities = frozenset({Capability.REFERENCES})

        def references(self, ctx, uri, line, character, include_declaration):
            return [Location(uri=uri, line=int(include_declaration))]

    uri = (work / "m.py").as_uri()

    async def go():
        async with lsp_session(Host({"r": Refs("r")}), [work]) as session:
            return [
                (
                    await session.client.text_document_references_async(
                        types.ReferenceParams(
                            text_document=types.TextDocumentIdentifier(uri=uri),
                            position=types.Position(line=0, character=0),
                            context=types.ReferenceContext(include_declaration=flag),
                        )
                    )
                )[0].range.start.line
                for flag in (False, True)
            ]

    assert run(go()) == [0, 1]


def test_folders_sharing_a_store_get_a_provider_per_guard(config, monkeypatch):
    """witan-council guards a fallback store's writes but not a deliberately
    chosen one's. Two folders on one store with different guards must share
    the store without sharing the first folder's guard."""
    work, home = config
    monkeypatch.chdir(work)
    plugin = Recorder(
        "p",
        {
            "work": Route(key="/stores/one", local=True, guard="deliberate"),
            "personal": Route(key="/stores/one", local=True, guard="fallback"),
        },
    )
    host = Host({"p": plugin})
    host.set_folders([resolve_folder(p.as_uri(), p, None) for p in (work, home)])
    for folder in host.folders.values():
        assert host.context("p", folder) is not None
    assert [route.guard for route, _ in plugin.opened] == ["deliberate", "fallback"]
    host.close()


class Lazy(Recorder):
    """Lenses that cost a read each, so they are sent unresolved."""

    capabilities = frozenset({Capability.HOVER, Capability.CODE_LENS})

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.resolved: list[tuple[int, str]] = []

    def hover(self, ctx, uri, line, character):
        return None

    def code_lenses(self, ctx, uri):
        return [
            Lens(line=1, data={"symbol": "f"}),
            Lens(line=3, title="cheap *bold*", command="witan.echo", arguments=(3,)),
        ]

    def resolve_lens(self, ctx, uri, lens):
        self.resolved.append((lens.line, lens.data["symbol"]))
        return Lens(
            line=lens.line,
            title=f"[{lens.data['symbol']}](https://evil) callers",
            command="witan.echo",
            arguments=(lens.data["symbol"],),
        )


def _lens_client() -> types.ClientCapabilities:
    return types.ClientCapabilities(
        text_document=types.TextDocumentClientCapabilities(
            code_lens=types.CodeLensClientCapabilities()
        )
    )


def _hover(session, uri, line):
    return session.client.text_document_hover_async(
        types.HoverParams(
            text_document=types.TextDocumentIdentifier(uri=uri),
            position=types.Position(line=line, character=0),
        )
    )


def _code_actions(session, uri, line):
    position = types.Position(line=line, character=0)
    return session.client.text_document_code_action_async(
        types.CodeActionParams(
            text_document=types.TextDocumentIdentifier(uri=uri),
            range=types.Range(start=position, end=position),
            context=types.CodeActionContext(diagnostics=[]),
        )
    )


def test_a_lens_with_data_is_resolved_only_when_asked(config, monkeypatch):
    work, _ = config
    monkeypatch.chdir(work)
    plugin = Lazy("lazy")
    uri = (work / "m.py").as_uri()

    async def go():
        async with lsp_session(
            Host({"z": plugin}), [work], client_capabilities=_lens_client()
        ) as session:
            assert session.capabilities.code_lens_provider.resolve_provider
            unresolved, cheap = await session.client.text_document_code_lens_async(
                types.CodeLensParams(
                    text_document=types.TextDocumentIdentifier(uri=uri)
                )
            )
            assert unresolved.command is None
            assert cheap.command.title == "cheap *bold*"
            assert plugin.resolved == []
            resolved = await session.client.code_lens_resolve_async(unresolved)
            assert plugin.resolved == [(1, "f")]
            assert resolved.range == unresolved.range
            assert (resolved.command.command, list(resolved.command.arguments)) == (
                "witan.echo",
                ["f"],
            )
            # A client with CodeLens gets no fallback in hover or code actions.
            assert await _hover(session, uri, 1) is None
            assert not await _code_actions(session, uri, 1)

    run(go())


def test_without_codelens_the_lenses_move_into_hover_and_code_actions(
    config, monkeypatch
):
    """eglot has no CodeLens, so a hover on a lens's line carries its title,
    and the lens's command is a code action."""
    work, _ = config
    monkeypatch.chdir(work)
    uri = (work / "m.py").as_uri()

    async def go():
        async with lsp_session(Host({"z": Lazy("lazy")}), [work]) as session:
            hover = await _hover(session, uri, 1)
            # Escaped: a lens title is plain text, and in hover it must stay so.
            assert hover.contents.value == escape_markdown("[f](https://evil) callers")
            assert await _hover(session, uri, 2) is None
            actions = await _code_actions(session, uri, 3)
            assert [(a.title, a.command, list(a.arguments)) for a in actions] == [
                ("cheap *bold*", "witan.echo", [3])
            ]

    run(go())


def test_a_plugin_reads_the_open_buffer_and_every_folder(config, monkeypatch):
    work, home = config
    monkeypatch.chdir(work)

    class Reads(Recorder):
        capabilities = frozenset({Capability.HOVER})

        def hover(self, ctx, uri, line, character):
            folders = ",".join(f.target for f in ctx.folders)
            return f"{ctx.text(uri).splitlines()[line]} in {folders}"

    path = work / "m.py"
    path.write_text("on disk\n")

    async def go():
        async with lsp_session(Host({"r": Reads("r")}), [work, home]) as session:
            await session.open(path, "in the buffer\n")
            return (await _hover(session, path.as_uri(), 0)).contents.value

    assert run(go()) == "in the buffer in work,personal"
    host = Host({"r": Reads("r")})
    host.set_folders([resolve_folder(work.as_uri(), work, None)])
    assert host.hover(path, path.as_uri(), 0, 0) == "on disk in work"
    host.close()


def test_an_unopened_document_reads_from_disk_and_a_missing_one_is_empty(
    config, monkeypatch
):
    """pygls builds an unopened document from disk and raises for a file that
    is gone; the host reads disk itself and answers empty instead."""
    work, _ = config
    monkeypatch.chdir(work)

    class Reads(Recorder):
        capabilities = frozenset({Capability.HOVER})

        def hover(self, ctx, uri, line, character):
            return repr(ctx.text(uri))

    present, missing = work / "present.py", work / "missing.py"
    present.write_text("on disk\n")

    async def go():
        async with lsp_session(Host({"r": Reads("r")}), [work]) as session:
            return [
                (await _hover(session, p.as_uri(), 0)).contents.value
                for p in (present, missing)
            ]

    assert run(go()) == ["'on disk\\n'", "''"]


def test_resolving_a_lens_that_has_its_command_returns_it(config, monkeypatch):
    """Only an unresolved lens carries data. A client that resolves one that
    already has its command gets it back rather than an error."""
    work, _ = config
    monkeypatch.chdir(work)
    lens = types.CodeLens(
        range=types.Range(start=types.Position(1, 0), end=types.Position(1, 0)),
        command=types.Command(title="cheap", command="witan.echo", arguments=[3]),
    )

    async def go():
        async with lsp_session(
            Host({"z": Lazy("lazy")}), [work], client_capabilities=_lens_client()
        ) as session:
            return await session.client.code_lens_resolve_async(lens)

    resolved = run(go())
    assert (resolved.command.title, resolved.data) == ("cheap", None)


def test_serve_keeps_stdin_away_from_prompts(monkeypatch):
    """stdin is the JSON-RPC channel. A plugin call that looks for a terminal
    to prompt on finds none, even when the server was started from one."""
    import io
    import sys

    from pygls.lsp.server import LanguageServer

    from witan_core.elicit import has_terminal
    from witan_core.lsp import server as server_module

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    stdin = Terminal("a person's answer\n")
    stdin.buffer = io.BytesIO()
    seen = {}

    def start_io(self, reader, writer):
        seen.update(
            channel=reader,
            terminal=has_terminal(),
            read=sys.stdin.read(),
            buffer=sys.stdin.buffer.read(),
        )

    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(server_module, "discover_plugins", dict)
    monkeypatch.setattr(LanguageServer, "start_io", start_io)
    server_module.serve()
    assert seen == {
        "channel": stdin.buffer,
        "terminal": False,
        "read": "",
        "buffer": b"",
    }
    assert sys.stdin is stdin


def test_plugins_see_the_editors_environment_from_before_serve_changed_it(
    monkeypatch, tmp_path
):
    """serve() drops the agent path variables before any plugin runs, so a
    plugin deciding what a chat-panel agent (which gets the editor's
    environment) would resolve has to be told what the editor had."""
    import os

    from pygls.lsp.server import LanguageServer

    from witan_core.lsp import server as server_module

    seen = {}

    class Environ(Plugin):
        commands = ("witan.environ",)

        def execute_command(self, ctx, command, arguments):
            return dict(ctx.editor_environ)

    def start_io(self, reader, writer):
        seen["process"] = {
            n: n in os.environ for n in ("CLAUDE_PROJECT_DIR", "WITAN_REPO")
        }

    def host(plugins, **kwargs):
        seen["host"] = Host(plugins, **kwargs)
        return seen["host"]

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("WITAN_REPO", "https://github.com/me/dots")
    monkeypatch.setattr(server_module, "discover_plugins", lambda: {"e": Environ()})
    monkeypatch.setattr(server_module, "Host", host)
    monkeypatch.setattr(LanguageServer, "start_io", start_io)
    server_module.serve()

    assert seen["process"] == {"CLAUDE_PROJECT_DIR": False, "WITAN_REPO": False}
    environ = seen["host"].execute("witan.environ", [])
    assert environ["CLAUDE_PROJECT_DIR"] == str(tmp_path)
    assert environ["WITAN_REPO"] == "https://github.com/me/dots"


def test_a_host_built_outside_serve_reads_the_live_environment(config, monkeypatch):
    work, _ = config
    monkeypatch.setenv("WITAN_REPO", "https://github.com/me/dots")

    class Environ(Plugin):
        commands = ("witan.environ",)

        def route(self, folder):
            return Route(key="remote", local=False)

        def open(self, route, folder):
            return self

        def execute_command(self, ctx, command, arguments):
            return ctx.editor_environ.get("WITAN_REPO")

    async def go():
        async with lsp_session(Host({"e": Environ()}), [work]) as session:
            return await session.command("witan.environ")

    assert run(go()) == "https://github.com/me/dots"


def test_plugins_get_columns_in_code_points_whatever_the_encoding(config, monkeypatch):
    """Neovim negotiates UTF-8, VS Code uses UTF-16. Column 2 after `é` is the
    second character either way once the host converts it."""
    work, _ = config
    monkeypatch.chdir(work)

    class Columns(Recorder):
        capabilities = frozenset({Capability.HOVER})

        def hover(self, ctx, uri, line, character):
            return ctx.text(uri).splitlines()[line][character]

    path = work / "m.py"
    utf8 = types.ClientCapabilities(
        general=types.GeneralClientCapabilities(
            position_encodings=[types.PositionEncodingKind.Utf8]
        )
    )

    async def go(capabilities, column):
        async with lsp_session(
            Host({"c": Columns("c")}), [work], client_capabilities=capabilities
        ) as session:
            await session.open(path, "é😋ab\n")
            hover = await session.client.text_document_hover_async(
                types.HoverParams(
                    text_document=types.TextDocumentIdentifier(uri=path.as_uri()),
                    position=types.Position(line=0, character=column),
                )
            )
            return hover.contents.value

    # `a` is after two bytes of `é` and four of `😋` in UTF-8, after one and
    # two code units in UTF-16.
    assert run(go(utf8, 6)) == "a"
    assert run(go(None, 3)) == "a"


@pytest.mark.parametrize(
    ("text", "markdown"),
    [
        ("[a](b)", r"\[a\]\(b\)"),
        ("![i](u)", r"\!\[i\]\(u\)"),
        ("<img src=x>", r"\<img src\=x\>"),
        ("&amp;", r"\&amp\;"),
        ("line\n# two", r"line \# two"),
        ("snake_case *x*", r"snake\_case \*x\*"),
        ("see https://x.example", r"see https\:\/\/x\.example"),
        ("ann@x.example", r"ann\@x\.example"),
        ("    indented\tcode", r"indented code"),
    ],
)
def test_escape_markdown_renders_text_as_itself(text, markdown):
    assert escape_markdown(text) == markdown
