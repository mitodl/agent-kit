"""The ``--target`` in effect for this process.

The twin of :mod:`witan.cli.selected_target`, and duplicated for the same
reason :mod:`witan_code.output` is: ``witan code …`` mounts this package's
cyclopts App but **not** its meta launcher, so witan's launcher is the one that
runs and it forwards what it bound into here. Two modules rather than one
because witan depends on witan-code, not the other way round.

Left ``None`` when this package's own binary is driven directly
(``witan-code index``): that launcher declares no ``--target``, so resolution
falls through to ``WITAN_TARGET`` and the checkout's ``match_*`` rules exactly
as it did before. Adding the option there is a separate change.
"""

from __future__ import annotations

_selected: str | None = None


def set_selected_target(name: str | None) -> None:
    """Record the ``--target`` the calling launcher bound, if any."""
    global _selected
    _selected = name


def selected_target() -> str | None:
    """The ``--target`` given on this command line, else ``None``."""
    return _selected
