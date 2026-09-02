"""The ``--target`` in effect for this process.

``--target`` used to be declared per command, on seven of them, which had two
consequences. Commands that did not declare it — ``witan tasks``, ``witan
memory``, and ``witan code index``, the one that writes code graphs — could
only be pointed at a target through ``WITAN_TARGET``. And the pre-dispatch
routing check in ``_launcher`` runs BEFORE ``app(tokens)`` binds a command's
arguments, so it could never see the flag at all: under ``witan whoami --target
qa`` it resolved the ambient target instead, warned about a target nobody asked
about, and stamped THAT target's throttle file — spending the once-a-day window
the next ambient command was supposed to get.

So the flag moved to the meta launcher, where cyclopts binds it before dispatch,
and this module is where it lands. Same shape as
:mod:`witan.cli.output`'s ``--output-format``, and for the same reason: an
app-level option has to reach commands that never declared it.

★ THE FLAG IS CONSUMED BY THE LAUNCHER, WHICH IS WHY THE SEVEN HAD TO GIVE IT
UP. A meta-level parameter is stripped from the tokens the subcommand parses, so
leaving ``target`` in those signatures would have left them binding ``None``
while the launcher held the real value — the command silently falling back to
the ambient target, which is the exact class of defect this reaches back to fix.
They now read :func:`selected_target` instead.

``migrate merge`` is the one command that kept a ``--target``-shaped argument,
renamed to ``--target-uri``: its value is a STORE URI, not a target name
(``--to`` is that command's target-name flag), so folding it in here would have
reinterpreted ``s3://bucket/graph.omni`` as a configured target.
"""

from __future__ import annotations

_selected: str | None = None


def set_selected_target(name: str | None) -> None:
    """Record the ``--target`` the launcher bound, if any."""
    global _selected
    _selected = name


def selected_target() -> str | None:
    """The ``--target`` given on this command line, else ``None``.

    ``None`` is not "no target" — it means nothing was named here, and
    resolution falls through to ``WITAN_TARGET`` and then the checkout's
    ``match_*`` rules. That fall-through lives in
    :func:`witan.config.load_remote_config`, which every caller reaches
    anyway; this only supplies the first tier.
    """
    return _selected
