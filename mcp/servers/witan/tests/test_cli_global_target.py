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
    """`witan tasks`/`memory`/`code index` could only be steered by the env var.

    Asserted through the accessor the commands call rather than by driving each
    one, since the point is that they all share a single resolution path.
    """
    monkeypatch.setattr("witan.cli.app", lambda tokens: None)
    _on_a_terminal(monkeypatch, False)

    _launcher("tasks", target="ci")

    assert selected_target() == "ci"


def test_the_auth_commands_read_the_launcher_bound_target(monkeypatch):
    """They dropped their own parameter — a meta-level flag is consumed by the
    launcher, so keeping it would have bound `None` here while the launcher
    held the real value, silently falling back to the ambient target."""
    from witan.cli import auth

    seen = []
    monkeypatch.setattr(auth, "_remote_or_exit", lambda t: seen.append(t) or _stop())

    set_selected_target("qa")
    with pytest.raises(_Stop):
        auth.whoami()

    assert seen == ["qa"]


class _Stop(Exception):
    """Ends the command once the assertion's evidence has been collected."""


def _stop():
    raise _Stop
