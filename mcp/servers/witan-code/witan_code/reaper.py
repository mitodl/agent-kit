"""Reaping stale branch views from a shared code graph.

Every developer's every git branch gets a view on the shared cluster graph
(:mod:`witan_code.views`) — that is the point of the decision to keep in-flight
work visible to everyone rather than hidden in a private store. It is also
unbounded: nothing about indexing a branch ever unindexes it, and a branch view
outlives the git branch it mirrors, the checkout that created it, and often the
person who created it. So the shared graph needs a sweeper, and the sweeper has
to live server-side.

It cannot be ``witan-code branches --prune`` wearing a different hat. That
command answers "which of *this checkout's* views no longer have a git branch",
which is a sound question about a store one machine writes and a meaningless one
about a store every user of the cluster writes: from here, "I don't have that
branch" and "that branch is gone" are the same observation, so pruning a shared
graph would delete other people's in-flight work. It refuses, and keeps
refusing. This module asks a different question that a shared graph *can*
answer: which views has nobody written in a long time.

Idleness is the signal because it is the only one the store actually has.
``omnigraph branch list`` returns bare names — no creation date, no owner, no
size — so age comes from the commit log
(:meth:`OmnigraphClient.branch_last_write`). Two consequences worth stating:

- **A view with no writes of its own is never reaped.** It holds nothing that
  isn't already on the branch it forked from, so reaping it would reclaim
  nothing, and there is no timestamp to distinguish one created ten seconds ago
  from one created a year ago. Deleting it would race the indexer that just
  created it and is about to write it.
- **Idleness is not abandonment.** A branch parked for a month and then picked
  back up loses its view, not its work; the next index rebuilds it from the
  checkout. Views are re-derivable caches — that is what makes deletion, rather
  than merge, the whole lifecycle (docs/BRANCH_INDEXING.md).

Authority: reaping deletes views this process does not own, which is exactly
what no ordinary client may do. Cedar grants ``branch_delete`` on unprotected
branches to ``witan-ci`` alone (policy/code-graph.policy.yaml), so on a shared
graph this runs as the CI indexer or not at all — and says so locally rather
than letting the server issue the denial. Against a local store there is no
policy engine and one user who owns everything, so it just runs.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import config as cfg_module
from . import views

__all__ = [
    "DEFAULT_MAX_IDLE_DAYS",
    "DEFAULT_SURVEY_WORKERS",
    "MAX_IDLE_ENV_VAR",
    "PROTECTED_VIEWS",
    "SURVEY_WORKERS_ENV_VAR",
    "ReapReport",
    "ViewAge",
    "max_idle_days",
    "reap",
    "select_stale",
    "survey_workers",
]

MAX_IDLE_ENV_VAR = "WITAN_CODE_VIEW_MAX_IDLE_DAYS"
"""Override the idle window. ``0`` (or negative) disables reaping entirely."""

DEFAULT_MAX_IDLE_DAYS = 14.0
"""Long enough to cover a two-week vacation, short enough to bound sprawl."""

SURVEY_WORKERS_ENV_VAR = "WITAN_CODE_VIEW_SURVEY_WORKERS"
"""How many views to age at once. ``1`` ages them one after another."""

DEFAULT_SURVEY_WORKERS = 8
"""Measured against production's bridge graph (309 views, omnigraph 0.11):
one at a time took 327s, 4 workers 85s, 8 workers 50s, and right after the
8-worker run ``kubectl top`` read the 1-CPU omnigraph-server at 780m. Kept
there rather than higher so the sweep leaves that server room for its other
readers."""

PROTECTED_VIEWS = frozenset({"main"})
"""Never reaped, whatever its age.

``main`` is the committed index every reader falls back to; it is idle by
design between merges, so idleness must not condemn it. Cedar refuses the
delete independently (no ``branch_delete`` on protected for any group) — this
is the client half of the same invariant, not the enforcement.
"""


def max_idle_days() -> float:
    """The configured idle window in days; ``0`` disables reaping."""
    raw = os.environ.get(MAX_IDLE_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_IDLE_DAYS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{MAX_IDLE_ENV_VAR}={raw!r} is not a number of days") from exc
    return max(value, 0.0)


def survey_workers() -> int:
    """The configured survey concurrency; anything below ``1`` means ``1``."""
    raw = os.environ.get(SURVEY_WORKERS_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_SURVEY_WORKERS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{SURVEY_WORKERS_ENV_VAR}={raw!r} is not a whole number of workers"
        ) from exc
    return max(value, 1)


@dataclass(frozen=True)
class ViewAge:
    """One branch view and when it was last written."""

    view: str
    last_write: float | None
    """Epoch seconds, or ``None`` for a view with no writes of its own."""

    @property
    def owner(self) -> str | None:
        """The actor that owns this view, or ``None`` if un-namespaced."""
        return views.owner(self.view)

    def idle_days(self, now: float) -> float | None:
        """Days since the last write, or ``None`` if it was never written."""
        return None if self.last_write is None else (now - self.last_write) / 86400.0


def select_stale(
    ages: Iterable[ViewAge],
    *,
    now: float,
    max_idle: float,
    protected: frozenset[str] = PROTECTED_VIEWS,
) -> list[ViewAge]:
    """The views in ``ages`` that should be reaped, oldest first.

    ``max_idle`` is in days; ``0`` or less selects nothing, so the env var can
    turn reaping off without the caller special-casing it. See the module
    docstring for why never-written views survive.
    """
    if max_idle <= 0:
        return []
    stale = [
        age
        for age in ages
        if age.view not in protected
        and age.last_write is not None
        and (now - age.last_write) / 86400.0 >= max_idle
    ]
    return sorted(stale, key=lambda age: age.last_write or 0.0)


@dataclass
class ReapReport:
    """What one sweep of one graph found and did."""

    graph: str
    now: float = 0.0
    """The instant the sweep aged views against — so a reader reporting "idle
    N days" quotes the number the decision was made on, not a fresher one."""
    scanned: int = 0
    stale: list[ViewAge] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    """``(view, error)`` for views that were stale but could not be deleted."""

    @property
    def kept(self) -> int:
        return self.scanned - len(self.stale)


def survey(
    client, *, now: float, max_idle: float, workers: int | None = None
) -> tuple[list[ViewAge], list[ViewAge]]:
    """Age every view on ``client``'s graph; return ``(all, stale)``.

    Read-only — the dry-run half of :func:`reap`, and what the CLI prints when
    it has not been told to delete anything.
    """
    names = [name for name in client.list_branches() if name not in PROTECTED_VIEWS]
    workers = survey_workers() if workers is None else workers
    last_writes = _last_writes(client, names, workers=workers)
    ages = [
        ViewAge(view=name, last_write=last_write)
        for name, last_write in zip(names, last_writes, strict=True)
    ]
    return ages, select_stale(ages, now=now, max_idle=max_idle)


def _last_writes(client, names: list[str], *, workers: int) -> list[float | None]:
    """``client.branch_last_write`` for each of ``names``, in order.

    Concurrent because the survey is one ``commit list --branch`` per view and
    each returns the view's whole reachable history, ``main``'s included — so
    the sweep costs views x commit-log length and both only grow. On omnigraph
    0.11 ``commit list`` without ``--branch`` returns ``main``'s commits alone
    and ``changes`` is per-branch too, so the calls can be overlapped but not
    merged. Serially, production's bridge graph alone outgrew the reaper
    CronJob's 600s deadline.

    The first failure propagates, as it did serially, and views not yet
    started are cancelled rather than aged for a survey that is already lost.
    """
    if workers <= 1 or len(names) <= 1:
        return [client.branch_last_write(name) for name in names]
    pool = ThreadPoolExecutor(max_workers=min(workers, len(names)))
    try:
        return list(pool.map(client.branch_last_write, names))
    finally:
        pool.shutdown(cancel_futures=True)


def reap(
    client,
    *,
    graph: str,
    now: float | None = None,
    max_idle: float | None = None,
    apply: bool = False,
    cfg: cfg_module.Config | None = None,
    workers: int | None = None,
) -> ReapReport:
    """Sweep one graph's stale branch views.

    Without ``apply`` this only reports — the destructive half is opt-in
    because the cost of a wrong idle window is other people's indexes, and the
    window is the one input nobody can validate from inside the store.

    Raises ``PermissionError`` when asked to delete from a shared graph as
    anything but the CI indexer; see the module docstring on authority.
    """
    now = time.time() if now is None else now
    max_idle = max_idle_days() if max_idle is None else max_idle
    cfg = cfg_module.load() if cfg is None else cfg

    # Authority first, and unconditionally: a job running with the wrong role
    # is a misconfiguration the operator should hear about even on a sweep that
    # would have deleted nothing.
    if apply and client.is_remote and not cfg.is_designated_writer:
        raise PermissionError(
            f"Refusing to reap views from the shared graph {graph}: they belong "
            f"to every user of the cluster, and only the CI indexer may delete "
            f"them. Set WITAN_CODE_INDEX_ROLE={cfg_module.INDEX_ROLE_CI} if this "
            "IS the reaper job; otherwise drop --apply and read the report."
        )

    # Disabled means disabled: `select_stale` would return nothing anyway, but
    # only after `survey` had already spent one `commit list` subprocess per
    # view. On a shared graph carrying every developer's every branch that is a
    # full scan to reach a foregone conclusion, on whatever schedule the job
    # runs. The empty report is honest — nothing was scanned.
    if max_idle <= 0:
        return ReapReport(graph=graph, now=now)

    ages, stale = survey(client, now=now, max_idle=max_idle, workers=workers)
    report = ReapReport(graph=graph, now=now, scanned=len(ages), stale=stale)
    if not apply:
        return report
    for age in stale:
        try:
            client.delete_branch(age.view)
        except RuntimeError as exc:
            # One unreapable view must not strand the rest of the sweep: a
            # scheduled job that aborts on the first failure never gets past it.
            report.failed.append((age.view, str(exc)))
        else:
            report.deleted.append(age.view)
    return report
