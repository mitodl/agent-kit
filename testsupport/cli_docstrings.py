"""Find CLI commands whose parameter docs cyclopts will drop from ``--help``.

cyclopts reads parameter help with ``docstring_parser.parse_from_object``, which
tries every style and keeps the parse with the most entries: numpydoc, for a
``Parameters`` / ``----------`` section. numpydoc reads
``name: text`` on one line as a parameter NAMED ``name`` whose TYPE is
``text``, so the description is only whatever continuation lines follow, and
``--help`` and ``docs/reference/cli.md`` show nothing or half a sentence. The
form it reads as a description puts the text on the next, indented line::

    slug:
        Project ``wp-`` slug.

No command here documents a parameter type in its docstring (the annotation
carries it), so any parsed type means the one-line form.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import docstring_parser
from cyclopts import App


def _commands(app: App, seen: set[int]) -> Iterator[Callable]:
    if id(app) in seen:
        return
    seen.add(id(app))
    if app.default_command is not None:
        yield app.default_command
    for name in app:
        yield from _commands(app[name], seen)


def one_line_param_docs(app: App, package: str) -> list[str]:
    """Return ``module.function: [params]`` for each command whose module name
    starts with ``package`` and whose docstring has a one-line ``name: text``
    parameter entry. ``"witan"`` also matches the mounted ``witan_code``
    commands.

    :param app: the root cyclopts app; its meta app's default command is
        included.
    :param package: only commands whose ``__module__`` starts with this are
        checked, which leaves out cyclopts' own ``--help``/``--version``.
    :returns: one entry per offending command, empty when there are none.
    :rtype: list[str]
    """
    found = list(_commands(app, set()))
    if app.meta.default_command is not None:
        found.append(app.meta.default_command)
    bad = []
    for fn in found:
        if not fn.__module__.startswith(package):
            continue
        params = docstring_parser.parse_from_object(fn).params
        typed = [p.arg_name for p in params if p.type_name]
        if typed:
            bad.append(f"{fn.__module__}.{fn.__qualname__}: {typed}")
    return bad
