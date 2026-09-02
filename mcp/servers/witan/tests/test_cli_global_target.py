"""``--target`` is an app-level option, bound before dispatch.

It used to be declared per command, on seven of them. That had two costs. The
commands that did NOT declare it — ``witan tasks``, ``witan memory``, and
``witan code index``, the one that writes code graphs — could only be pointed at
a target through ``WITAN_TARGET``. And the pre-dispatch routing check runs
before ``app(tokens)`` binds a command's arguments, so it could not see the flag
at all: it resolved the ambient target instead, warned about one nobody asked
about, and stamped THAT target's throttle file.
"""

from __future__ import annotations

import pytest

from witan.cli import _warn_about_routing, _launcher
from witan.cli.selected_target import selected_target, set_selected_target


@pytest.fixture(autouse=True)
def _clean_selection():
    """Module-level state, so a leaked value would silently steer a later test."""
    set_selected_target(None)
    yield
    set_selected_target(None)


def _on_a_terminal(monkeypatch, is_terminal: bool):
    from witan.cli import _common

    monkeypatch.setattr(
        type(_common.stderr_console), "is_terminal", property(lambda self: is_terminal)
    )


def test_the_launcher_records_the_target_before_dispatch(monkeypatch):
    """The whole point: bound by the meta launcher, so the routing check below
    it can see it. `app(tokens)` — the call that binds a command's own
    arguments — happens after."""
    seen = {}
    monkeypatch.setattr(
        "witan.cli.app", lambda tokens: seen.update(at_dispatch=selected_target())
    )
    _on_a_terminal(monkeypatch, False)  # keep the warning out of this test

    _launcher("tasks", target="qa")

    assert seen["at_dispatch"] == "qa"
    assert selected_target() == "qa"


def test_no_flag_leaves_resolution_to_the_env_and_the_checkout(monkeypatch):
    """`None` is not "no target" — it means nothing was named here, and
    `load_remote_config` falls through to WITAN_TARGET then `match_*`."""
    monkeypatch.setattr("witan.cli.app", lambda tokens: None)
    _on_a_terminal(monkeypatch, False)

    _launcher("tasks")

    assert selected_target() is None


def test_the_routing_check_is_told_which_target(monkeypatch):
    """Regression for the defect this reaches back to fix. The check used to
    take no target, resolve the ambient one, and stamp its throttle file —
    silencing the next ambient run for a day over a command that named a
    different target."""
    calls = []
    monkeypatch.setattr(
        "witan.cli.warn_if_code_graph_is_local", lambda **kw: calls.append(kw)
    )
    _on_a_terminal(monkeypatch, True)
    set_selected_target("qa")

    _warn_about_routing(("whoami",))

    assert calls == [{"throttle": True, "target": "qa"}]


def test_an_explicit_target_is_no_longer_skipped(monkeypatch):
    """#316 worked around the blindness by skipping the warning entirely when
    `--target` appeared in argv. Now that the launcher binds it, the check runs
    and answers for the named target instead."""
    calls = []
    monkeypatch.setattr(
        "witan.cli.warn_if_code_graph_is_local", lambda **kw: calls.append(kw)
    )
    _on_a_terminal(monkeypatch, True)
    set_selected_target("qa")

    # The flag itself never reaches these tokens any more — the launcher
    # consumed it — which is exactly why the old argv scan was needed.
    _warn_about_routing(("whoami",))

    assert calls == [{"throttle": True, "target": "qa"}]


def test_serve_still_warns_for_itself(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "witan.cli.warn_if_code_graph_is_local", lambda **kw: calls.append(kw)
    )
    _on_a_terminal(monkeypatch, True)
    set_selected_target("qa")

    _warn_about_routing(("serve", "--transport", "stdio"))

    assert calls == []


def test_commands_that_never_had_the_flag_now_read_it(monkeypatch):
    """`witan tasks`/`memory`/`projects` could only be steered by the env var.

    Driven through ``_srv()`` — the resolution path those commands actually
    dispatch through — and NOT through the accessor. Asserting the accessor is
    what made the first cut of this change look correct while
    ``witan --target ci tasks`` still read production: the launcher recorded
    the flag faithfully and nothing on the dispatch path ever asked for it.
    """
    from witan import config as cfg_module
    from witan.cli import _common

    seen = {}
    monkeypatch.setattr(_common, "_server", None)
    monkeypatch.setattr(
        cfg_module,
        "load_remote_config",
        lambda target=None: seen.setdefault("remote", target),
    )
    monkeypatch.setattr(_common, "remote_proxy", lambda remote: remote)
    set_selected_target("ci")

    _common._srv()

    assert seen["remote"] == "ci"


def test_the_local_dispatch_diagnosis_gets_the_same_target(monkeypatch):
    """The other half of ``_srv()``. This one decides whether opening the local
    store was deliberate, and a target named on the command line is one of the
    three things that make it so — diagnosing against the ambient target would
    call a deliberate local dispatch accidental, and vice versa."""
    from witan import config as cfg_module
    from witan.cli import _common

    seen = {}
    monkeypatch.setattr(_common, "_server", None)
    monkeypatch.setattr(cfg_module, "load_remote_config", lambda target=None: None)
    monkeypatch.setattr(
        cfg_module,
        "diagnose_local_dispatch",
        lambda target=None: seen.setdefault("diagnosed", target),
    )
    monkeypatch.setattr(
        "witan.cli.local_dispatch.local_server", lambda diagnosis: diagnosis
    )
    set_selected_target("work")

    _common._srv()

    assert seen["diagnosed"] == "work"


def test_an_unknown_target_is_refused_rather_than_ignored(monkeypatch):
    """The tell that the flag was never being consulted: a name that matches no
    ``[targets.<name>]`` block produced a normal table off the ambient target
    instead of an error, because ``_select_target`` was never asked about it."""
    from witan.cli import _common

    monkeypatch.setattr(_common, "_server", None)
    set_selected_target("nosuchtarget")

    with pytest.raises(SystemExit):
        _common._srv()


def test_serve_resolves_the_named_target_too(monkeypatch):
    """`serve` is the MCP surface every agent session talks to, and agent-kit
    #261 is what happens when it resolves a different target from the CLI in
    the same directory: the hook read the deployment while the agent's tools
    wrote the laptop. It is exempt from the routing WARNING (it has no reader),
    never from the routing itself."""
    from witan import config as cfg_module
    from witan.cli import _serve_target

    seen = {}

    def _record(target=None):
        seen["target"] = target
        return None  # local branch; which server comes back is not the point

    monkeypatch.setattr(cfg_module, "load_remote_config", _record)
    set_selected_target("qa")

    _serve_target("stdio")

    assert seen["target"] == "qa"


def test_the_launcher_forwards_the_target_into_witan_code(monkeypatch):
    """`witan code …` mounts witan-code's App but not its meta launcher, so
    this forwarding is the only way the flag reaches `witan code index` — the
    command that WRITES a code graph, and the main prize of moving the flag.

    Stubbed into ``sys.modules`` rather than imported: witan-code is not a
    dependency of witan-council's test environment (the launcher's own import
    sits under ``except ImportError``), so a real import here would be a test
    that never runs in CI. Same pattern as the ``--output-format`` twin in
    test_cli_output_format.py.
    """
    import sys
    import types

    import witan.cli as cli_module

    forwarded = []
    fake_pkg = types.ModuleType("witan_code")
    fake_output = types.ModuleType("witan_code.output")
    fake_output.set_output_format = lambda fmt: None
    fake_target = types.ModuleType("witan_code.selected_target")
    fake_target.set_selected_target = forwarded.append
    monkeypatch.setitem(sys.modules, "witan_code", fake_pkg)
    monkeypatch.setitem(sys.modules, "witan_code.output", fake_output)
    monkeypatch.setitem(sys.modules, "witan_code.selected_target", fake_target)
    monkeypatch.setattr(cli_module, "app", lambda tokens: None)
    _on_a_terminal(monkeypatch, False)

    cli_module._launcher("code", "index", ".", target="qa")

    assert forwarded == ["qa"]


class _Stop(Exception):
    """Ends the command once the assertion's evidence has been collected."""


def _stop():
    raise _Stop
