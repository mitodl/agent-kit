"""The local-code-graph warning has to reach somebody.

The detection was never wrong; it was emitted only from ``witan serve``, whose
stderr belongs to the agent harness and in practice has no reader. These tests
pin the two properties that make the wider placement worth having: it fires for
the people who are actually misrouted, and it stays quiet for everyone else —
including the machines (hooks, CI, pipes) whose one warning would otherwise be
spent on nobody.
"""

from __future__ import annotations

import sys
import types

import pytest


class _FakeCodeConfig:
    def __init__(
        self,
        *,
        code_transport="direct",
        code_server=None,
        code_dir="/home/dev/.local/share/witan-code",
        target_name="production",
    ):
        self.code_transport = code_transport
        self.code_server = code_server
        self.code_dir = code_dir
        self.target_name = target_name

    @property
    def is_cluster(self) -> bool:
        # Mirrors witan_code.config.Config.is_cluster — a shared code graph is
        # shared either way it is reached.
        return self.code_server is not None or self.code_transport == "mcp"


@pytest.fixture
def code_installed(monkeypatch):
    """Stand in for witan-code, which the council's test env does not install.

    Injecting the module rather than skipping keeps this under test where it
    matters: the routing warning is about a config witan-code owns, and a test
    that skips wherever witan-code is absent is no test of the wiring at all.
    """

    def _install(cfg, *, raises=None):
        pkg = types.ModuleType("witan_code")
        mod = types.ModuleType("witan_code.config")

        def load(target=None):
            if raises is not None:
                raise raises
            return cfg

        mod.load = load
        mod.CODE_TRANSPORT_MCP = "mcp"
        pkg.config = mod
        monkeypatch.setitem(sys.modules, "witan_code", pkg)
        monkeypatch.setitem(sys.modules, "witan_code.config", mod)
        return cfg

    return _install


@pytest.fixture
def deployed(monkeypatch):
    """A configured deployment, which is what makes a local code graph odd."""
    from witan import config as cfg_module

    remote = cfg_module.RemoteConfig(
        url="https://witan.example/mcp",
        oidc_issuer="https://sso.example/realms/ol",
        target_name="production",
    )
    monkeypatch.setattr(cfg_module, "load_remote_config", lambda *a, **k: remote)
    return remote


@pytest.fixture
def warned(monkeypatch, tmp_path, capsys):
    """Run the warning against an isolated throttle stamp; return the stderr."""
    from witan import session_state
    from witan.cli import code_routing

    monkeypatch.setattr(session_state, "session_state_dir", lambda: tmp_path)
    # Rich suppresses colour off a tty but still renders the text; the assertions
    # below are on words, so no terminal emulation is needed.

    def _run(**kwargs):
        capsys.readouterr()
        code_routing.warn_if_code_graph_is_local(**kwargs)
        return capsys.readouterr().err

    return _run


# ── Who gets warned ──────────────────────────────────────────────────────────


def test_a_deployed_memory_graph_with_local_code_graphs_warns(
    deployed, code_installed, warned
):
    code_installed(_FakeCodeConfig())

    assert "code graphs are local" in warned()


def test_the_warning_names_the_target_to_edit(deployed, code_installed, warned):
    code_installed(_FakeCodeConfig(target_name="production"))

    # The one part of the sentence that says where to make the change.
    assert "production" in warned()


def test_code_transport_mcp_is_not_warned_about(deployed, code_installed, warned):
    code_installed(_FakeCodeConfig(code_transport="mcp"))

    assert warned() == ""


def test_a_direct_code_server_is_not_warned_about(deployed, code_installed, warned):
    """`code_server` is the other way to reach a SHARED graph, and it was a
    false positive: the check asked only about `code_transport`, so someone
    already sharing their code graphs got told they were not."""
    code_installed(_FakeCodeConfig(code_server="https://omnigraph.example"))

    assert warned() == ""


def test_a_purely_local_install_is_not_warned_about(
    monkeypatch, code_installed, warned
):
    """No deployment means local code graphs are the whole configuration, not a
    mismatch — and this is the majority of runs, so a warning here is the noise
    that gets the real one filtered out."""
    from witan import config as cfg_module

    monkeypatch.setattr(cfg_module, "load_remote_config", lambda *a, **k: None)
    code_installed(_FakeCodeConfig())

    assert warned() == ""


def test_witan_code_not_installed_says_nothing(deployed, warned, monkeypatch):
    monkeypatch.setitem(sys.modules, "witan_code", None)

    assert warned() == ""


def test_an_unparseable_code_config_is_left_to_the_real_command(
    deployed, code_installed, warned
):
    """A bad `code_transport` raises on the same load the command itself does,
    with a message naming the key that is wrong. Pre-empting it with a routing
    warning would bury the one line that fixes the problem."""
    code_installed(None, raises=ValueError("Unknown code_transport 'mpc'"))

    assert warned() == ""


# ── How often ────────────────────────────────────────────────────────────────


def test_the_throttled_warning_fires_once_and_then_stays_quiet(
    deployed, code_installed, warned
):
    code_installed(_FakeCodeConfig())

    assert "code graphs are local" in warned(throttle=True)
    assert warned(throttle=True) == ""


def test_an_unthrottled_warning_never_consumes_the_window(
    deployed, code_installed, warned
):
    """`witan serve` warns unthrottled AND must not spend the human's warning:
    its stderr may have no reader, so a serve run that recorded a warning would
    buy silence for exactly the person the warning is for."""
    code_installed(_FakeCodeConfig())

    assert "code graphs are local" in warned()
    assert "code graphs are local" in warned()
    assert "code graphs are local" in warned(throttle=True)


def test_switching_target_warns_again_inside_the_window(
    deployed, code_installed, warned
):
    code_installed(_FakeCodeConfig(target_name="production"))
    assert warned(throttle=True) != ""

    code_installed(_FakeCodeConfig(target_name="qa"))
    assert "qa" in warned(throttle=True)


def test_the_throttle_can_be_disabled_outright(
    deployed, code_installed, warned, monkeypatch
):
    from witan.cli import code_routing

    monkeypatch.setenv(code_routing.WARN_INTERVAL_ENV_VAR, "0")
    code_installed(_FakeCodeConfig())

    assert warned(throttle=True) == ""


# ── The dispatch-path gates ──────────────────────────────────────────────────


@pytest.fixture
def routing_calls(monkeypatch):
    """Record what `_warn_about_routing` decides, without touching config."""
    from witan import cli as cli_module

    calls = []
    monkeypatch.setattr(
        cli_module, "warn_if_code_graph_is_local", lambda **kw: calls.append(kw)
    )
    return calls


def _on_a_terminal(monkeypatch, is_terminal: bool):
    from witan.cli import _common

    monkeypatch.setattr(
        type(_common.stderr_console), "is_terminal", property(lambda self: is_terminal)
    )


def test_an_ordinary_command_warns_on_a_terminal(monkeypatch, routing_calls):
    from witan.cli import _warn_about_routing

    _on_a_terminal(monkeypatch, True)
    _warn_about_routing(("tasks",))

    assert routing_calls == [{"throttle": True}]


def test_nothing_is_said_when_no_person_is_reading(monkeypatch, routing_calls):
    """The Stop hook, the context hook and `witan code index .` in CI all run
    this CLI with nobody watching. Warning there is not merely useless — it
    would consume the throttle window and silence the human's next run."""
    from witan.cli import _warn_about_routing

    _on_a_terminal(monkeypatch, False)
    _warn_about_routing(("tasks",))

    assert routing_calls == []


def test_serve_is_left_to_warn_for_itself(monkeypatch, routing_calls):
    from witan.cli import _warn_about_routing

    _on_a_terminal(monkeypatch, True)
    # `--output-format json` is bound by the meta launcher's own signature and
    # never reaches here, so the command name is token 0.
    _warn_about_routing(("serve", "--transport", "stdio"))

    assert routing_calls == []


# ── `witan whoami` answering the other half ──────────────────────────────────


def test_whoami_reports_a_local_code_graph_by_path(code_installed):
    from witan.cli.code_routing import code_graph_destination

    code_installed(_FakeCodeConfig(code_dir="/home/dev/.local/share/witan-code"))

    assert code_graph_destination() == (
        "local to this machine, under /home/dev/.local/share/witan-code"
    )


def test_whoami_reports_code_graphs_routed_through_the_endpoint(code_installed):
    from witan.cli.code_routing import code_graph_destination

    code_installed(_FakeCodeConfig(code_transport="mcp"))

    assert "shared" in code_graph_destination()


def test_whoami_reports_a_directly_addressed_code_server(code_installed):
    from witan.cli.code_routing import code_graph_destination

    code_installed(_FakeCodeConfig(code_server="https://omnigraph.example"))

    assert "https://omnigraph.example" in code_graph_destination()


def test_whoami_asks_about_the_target_it_resolved_the_identity_for(code_installed):
    """Re-selecting would answer for a DIFFERENT target than the one whoami is
    reporting on — `witan whoami --target qa` must not describe production's
    code routing."""
    seen = []
    cfg = _FakeCodeConfig()
    code_installed(cfg)

    import witan_code.config as code_cfg_module

    real_load = code_cfg_module.load
    code_cfg_module.load = lambda target=None: (seen.append(target), real_load())[1]
    try:
        from witan.cli.code_routing import code_graph_destination

        code_graph_destination("qa")
    finally:
        code_cfg_module.load = real_load

    assert seen == ["qa"]


def test_whoami_says_nothing_when_witan_code_is_absent(monkeypatch):
    from witan.cli.code_routing import code_graph_destination

    monkeypatch.setitem(sys.modules, "witan_code", None)

    assert code_graph_destination() is None
