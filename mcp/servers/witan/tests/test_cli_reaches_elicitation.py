"""The CLI reaches the server's elicitation prompts.

``task_claim`` offers to steal a contended claim instead of flatly refusing —
the nicest recovery path in the whole claim surface, naming the holder and the
claim time. It was unreachable for every local CLI user, because ``_fn`` called
the tool with no ``ctx`` and ``elicit.confirm`` therefore took its
non-interactive default every time. Nothing said so; the code read as
supported.

The unit-level contract is in ``witan-core``'s ``test_console_elicitation.py``.
This drives the real ``_fn`` against the real ``task_claim`` so the wiring —
not just the helper — is pinned.
"""

import pytest

from .conftest import requires_omnigraph


class _Tty:
    def isatty(self):
        return True


@pytest.fixture
def answers(monkeypatch):
    """Type ``answer`` at whatever prompt the tool puts up."""

    def _at_the_prompt(answer: str | None):
        monkeypatch.setattr("sys.stdin", _Tty() if answer is not None else None)
        monkeypatch.setattr("builtins.input", lambda _prompt: answer or "")

    return _at_the_prompt


def _held_task(srv, fn):
    """A task claimed by somebody else, so the next claim is contended."""
    task = fn(srv.task_create)(title="contended", description="x")
    assert fn(srv.task_claim)(slug=task["slug"], assignee="someone-else")["claimed"]
    return task["slug"]


@requires_omnigraph
def test_cli_reaches_the_steal_prompt_and_a_yes_steals(server, answers):
    from witan import server as srv
    from witan.cli._common import _fn

    monkeypatch_free_slug = _held_task(server, _fn)
    answers("y")

    result = _fn(srv.task_claim)(slug=monkeypatch_free_slug, assignee="me")

    assert result["claimed"] is True
    assert result["stole"] is True


@requires_omnigraph
def test_a_no_at_the_prompt_declines_the_steal(server, answers):
    from witan import server as srv
    from witan.cli._common import _fn

    slug = _held_task(server, _fn)
    answers("n")

    result = _fn(srv.task_claim)(slug=slug, assignee="me")

    assert result["claimed"] is False
    assert result["reason"] == "held"


@requires_omnigraph
def test_without_a_terminal_the_historical_refusal_is_unchanged(server, answers):
    """The additive guarantee: an agent, a hook or a CI run has no tty, so it
    gets the flat refusal it always got rather than blocking on a prompt."""
    from witan import server as srv
    from witan.cli._common import _fn

    slug = _held_task(server, _fn)
    answers(None)

    result = _fn(srv.task_claim)(slug=slug, assignee="me")

    assert result["claimed"] is False
    assert result["reason"] == "held"
