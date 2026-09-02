"""``witan code …`` dispatches to the target witan's launcher bound.

This package's App is mounted by witan's CLI but its meta launcher is not, so
witan's launcher forwards the ``--target`` it bound into
:mod:`witan_code.selected_target` (the same forwarding ``--output-format``
already gets, for the same reason). What is asserted here is the other end of
that wire: that the dispatch path actually consults it.

Resolving the ambient target here instead is not a cosmetic gap. ``witan code
index`` WRITES a code graph, so it would have written to whichever deployment
the checkout happened to match while the flag on the command line said
otherwise — and said nothing about it.
"""

from __future__ import annotations

import pytest

from witan_code import cli as code_cli
from witan_code import config as code_cfg
from witan_code.selected_target import selected_target, set_selected_target


@pytest.fixture(autouse=True)
def _clean_selection():
    """Module-level state, so a leaked value would silently steer a later test."""
    set_selected_target(None)
    yield
    set_selected_target(None)


def test_dispatch_reads_the_forwarded_target(monkeypatch):
    seen = {}

    def _record(target=None):
        seen["target"] = target
        return None  # in-process branch; a stub RemoteConfig is not the point

    monkeypatch.setattr(code_cli, "_server", None)
    monkeypatch.setattr(code_cfg, "load_remote_config", _record)
    set_selected_target("qa")

    code_cli._srv()

    assert seen["target"] == "qa"


def test_no_forwarded_target_leaves_resolution_where_it_was(monkeypatch):
    """``None`` is not "no target": the standalone ``witan-code`` launcher
    declares no ``--target``, and resolution falls through to ``WITAN_TARGET``
    and the checkout's ``match_*`` rules exactly as it always did."""
    seen = {}

    def _record(target=None):
        seen["target"] = target
        return None

    monkeypatch.setattr(code_cli, "_server", None)
    monkeypatch.setattr(code_cfg, "load_remote_config", _record)

    code_cli._srv()

    assert seen["target"] is None


def test_the_auth_path_reads_it_too(monkeypatch):
    """``witan code login`` has to reach the same deployment the read commands
    do, or it mints a token against one target and spends it on another."""
    seen = {}

    def _record(target=None):
        seen["target"] = target
        return None

    monkeypatch.setattr(code_cfg, "load_remote_config", _record)
    set_selected_target("ci")

    with pytest.raises(SystemExit):
        code_cli._remote_or_exit()  # no remote configured -> clean exit

    assert seen["target"] == "ci"


def test_accessor_round_trips():
    set_selected_target("production")
    assert selected_target() == "production"
