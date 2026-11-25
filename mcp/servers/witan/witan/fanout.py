"""A workflow project's fan-out policy: whether runners may work its ready tasks.

The policy is four fields on ``WorkflowProject``: ``fanout_enabled``,
``fanout_concurrency`` (the cap on runner-claimed tasks in flight, across every
runner), ``fanout_harnesses`` (which harnesses a runner may start for it) and
``fanout_labels`` (labels a runner must advertise, e.g. ``docker``).
``fanout_set_by`` is the server-resolved actor who last changed any of them.

A runner claims only from a project whose policy :meth:`FanoutPolicy.admits`
it. A project that never had fan-out turned on reads as disabled, so it is
never claimed from.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

# The harnesses a runner can start. Mirrors witan_core.cli.AGENT_NAMES (minus
# its "all"), which a test holds this to.
Harness = Literal["claude", "pi", "codex", "copilot", "opencode"]
HARNESSES: tuple[str, ...] = get_args(Harness)

# What an enabled project with no recorded cap allows in flight: one task, so
# turning fan-out on never runs more than the sequential loop did.
DEFAULT_CONCURRENCY = 1

FIELDS = ("fanout_enabled", "fanout_concurrency", "fanout_harnesses", "fanout_labels")


def _names(items: Iterable[str]) -> list[str] | None:
    """Stripped, de-duplicated names in first-seen order; ``None`` when empty."""
    out: list[str] = []
    for item in items:
        name = item.strip()
        if name and name not in out:
            out.append(name)
    return out or None


def validated(
    *,
    enabled: bool | None = None,
    concurrency: int | None = None,
    harnesses: Iterable[str] | None = None,
    labels: Iterable[str] | None = None,
) -> dict[str, Any]:
    """The fan-out fields a caller passed, validated, keyed by column.

    An argument left ``None`` is absent from the result, so an update writes
    only what was passed. An empty ``harnesses`` or ``labels`` list is present
    as ``None``, which clears it.

    :param enabled: Whether runners may claim from the project.
    :param concurrency: Cap on runner-claimed tasks in flight; at least 1.
    :param harnesses: Harnesses a runner may start; each one of :data:`HARNESSES`.
    :param labels: Labels a runner must advertise.
    :returns: Column name to value, for the passed arguments only.
    :rtype: dict[str, Any]
    :raises ValueError: On a concurrency below 1 or an unknown harness.
    """
    out: dict[str, Any] = {}
    if enabled is not None:
        out["fanout_enabled"] = bool(enabled)
    if concurrency is not None:
        # bool is an int subclass, and True would otherwise read as a cap of 1.
        # A float (even 2.0) is refused too: the column is an I64.
        if (
            not isinstance(concurrency, int)
            or isinstance(concurrency, bool)
            or concurrency < 1
        ):
            msg = f"fan-out concurrency must be a positive integer, not {concurrency!r}"
            raise ValueError(msg)
        out["fanout_concurrency"] = concurrency
    if harnesses is not None:
        names = _names(harnesses)
        unknown = [h for h in names or [] if h not in HARNESSES]
        if unknown:
            msg = (
                f"unknown fan-out harness {', '.join(map(repr, unknown))}; "
                f"expected any of {', '.join(HARNESSES)}"
            )
            raise ValueError(msg)
        out["fanout_harnesses"] = names
    if labels is not None:
        out["fanout_labels"] = _names(labels)
    return out


@dataclass(frozen=True)
class FanoutPolicy:
    """The fan-out policy read off a project row.

    :param enabled: Whether runners may claim from the project at all.
    :param concurrency: Cap on runner-claimed tasks in flight, across runners.
    :param harnesses: Harnesses allowed, in the project's order; empty is any.
    :param labels: Labels a runner must advertise to claim from the project.
    :param set_by: Who last changed any fan-out field, or ``None``.
    """

    enabled: bool
    concurrency: int
    harnesses: tuple[str, ...]
    labels: tuple[str, ...]
    set_by: str | None

    @classmethod
    def of(cls, project: Mapping[str, Any]) -> FanoutPolicy:
        """Read the policy off a ``WorkflowProject`` row.

        Indexes the fields rather than defaulting them, so a projection that
        forgot to select them fails here instead of reading as disabled.
        """
        return cls(
            enabled=project["fanout_enabled"] is True,
            concurrency=project["fanout_concurrency"] or DEFAULT_CONCURRENCY,
            harnesses=tuple(project["fanout_harnesses"] or ()),
            labels=tuple(project["fanout_labels"] or ()),
            set_by=project["fanout_set_by"],
        )

    def allowed_harnesses(self, runner_harnesses: Iterable[str]) -> tuple[str, ...]:
        """The runner's harnesses this project allows, in the project's order.

        :param runner_harnesses: Harnesses installed on the runner.
        :returns: The allowed subset; the runner's own order when the project
            allows any harness.
        :rtype: tuple[str, ...]
        """
        have = tuple(dict.fromkeys(runner_harnesses))
        if not self.harnesses:
            return have
        return tuple(h for h in self.harnesses if h in have)

    def admits(self, *, harnesses: Iterable[str], labels: Iterable[str]) -> bool:
        """Whether a runner with these harnesses and labels may claim from it.

        :param harnesses: Harnesses installed on the runner.
        :param labels: Labels the runner advertises.
        :returns: True only when fan-out is enabled, the runner has an allowed
            harness, and it carries every required label.
        :rtype: bool
        """
        return (
            self.enabled
            and bool(self.allowed_harnesses(harnesses))
            and set(self.labels) <= set(labels)
        )


def enabled_projects(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The active projects with fan-out turned on: the only ones to claim from.

    :param rows: ``WorkflowProject`` rows carrying ``status`` and the fan-out
        fields.
    :returns: The rows a runner may consider, in input order.
    :rtype: list
    """
    return [r for r in rows if r["status"] == "active" and FanoutPolicy.of(r).enabled]


def describe(project: Mapping[str, Any]) -> str | None:
    """One line describing the policy, or ``None`` when no field was ever set."""
    if all(project.get(f) is None for f in FIELDS):
        return None
    p = FanoutPolicy.of(project)
    parts = [
        f"Fan-out {'on' if p.enabled else 'off'}",
        f"up to {p.concurrency} task{'s' if p.concurrency != 1 else ''} at once",
        f"harnesses {', '.join(p.harnesses) if p.harnesses else 'any'}",
    ]
    if p.labels:
        parts.append(f"runner labels {', '.join(p.labels)}")
    return f"{', '.join(parts)} (set by {p.set_by or 'an unknown user'})"
