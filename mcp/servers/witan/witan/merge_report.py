"""Whether a merge accounted for every row the source held.

``witan migrate merge`` reports what it *decided* — added, updated, kept — and
none of those numbers answers the question a person actually has after
migrating their store into a shared deployment: **did all of it arrive?**

The documented way to answer it was to export both graphs and compare ``jq``'d
type counts. That is unavailable to exactly the person who most needs it: once
the target is the deployed graph, the data tier is ClusterIP-only and an
ordinary user holds no omnigraph bearer token, so they cannot export the thing
they just wrote to.

So the merge answers it itself, out of numbers it already computes. There are
TWO questions, they fail in different places, and collapsing them into one
verdict would hide whichever failure the other's arithmetic happens to absorb.

★ DECIDED — every source record reached a reconciliation decision::

    source_rows = added + updated + kept_target + passthrough + duplicate_slugs

``added``/``updated``/``kept_target``
    one per distinct ``(type, slug)`` node — the reconciliation decision.
``passthrough``
    rows with no slug to reconcile on: edge rows, and any typed row exported
    without a slug. Loaded additively, never reconciled.
``duplicate_slugs``
    rows collapsed onto an earlier row sharing their ``(type, slug)``. Zero for
    a real ``omnigraph export`` (slug is the key), non-zero only for a
    hand-assembled source — and counted rather than ignored precisely so that
    case does not read as a shortfall. A check that cries wolf on a legitimate
    source is worse than no check.

★ WRITTEN — every row a decision called for was loaded::

    rows_loaded = added + updated + passthrough

(the winners plus the pass-through rows; ``kept_target`` writes nothing by
definition, and neither does a collapsed duplicate.)

WHY BOTH, AND WHY THE ORDER MATTERS. The two merge transports stop in
different halves. Over the deployment each batch reconciles *and* writes
server-side, so a batch that fails returns nothing and its rows never reach a
bucket — the DECIDED sum falls short. In process, reconciliation runs to
completion over the whole export before the first load, so a failed load
leaves every source row decided and the DECIDED check passes on a merge that
wrote half its rows; only the WRITTEN check sees it. Reporting just one of
them would have called that merge complete.

A dry run is checked for DECIDED only. It writes nothing on purpose, so
holding it to the written identity would report every plan as a failure.

Pure, and side-effect free at import, so ``witan.cli`` can use it without
pulling in ``witan.server`` — importing that module creates and schema-applies
a local store (see ``witan.cli.local_dispatch``).

★ FAILS SOFT, LIKE :mod:`witan.merge_watermark`. A deployment older than these
fields returns no ``source_rows``, and the right answer is then "cannot tell",
never a computed zero: defaulting the missing counts would report every edge
row in the source as unaccounted for and send someone hunting a merge failure
that did not happen.
"""

from __future__ import annotations

#: The buckets a source row can land in. Their sum is what ``source_rows`` is
#: checked against.
ACCOUNTED_FIELDS = (
    "added",
    "updated",
    "kept_target",
    "passthrough",
    "duplicate_slugs",
)

#: The buckets whose rows a merge actually writes. Their sum is what
#: ``rows_loaded`` is checked against.
WRITTEN_FIELDS = ("added", "updated", "passthrough")


def accounting(result: dict) -> dict | None:
    """Reconcile a merge result against the rows its source held.

    Returns ``None`` when the result carries no ``source_rows`` — an older
    deployment, or a client that could not total one because a batch came back
    without the fields. That is reported as "cannot tell" rather than guessed.

    Otherwise:

    ``source_rows`` / ``decided`` / ``undecided``
        the DECIDED identity and its shortfall.
    ``rows_loaded`` / ``expected_loaded`` / ``unwritten``
        the WRITTEN identity and its shortfall. ``None`` for a dry run, which
        writes nothing by design.
    ``complete``
        both identities hold (only the decided one, for a dry run).
    ``breakdown``
        the per-bucket counts, so a report can show its working.
    """
    source_rows = result.get("source_rows")
    if not isinstance(source_rows, int):
        return None
    breakdown = {field: int(result.get(field) or 0) for field in ACCOUNTED_FIELDS}
    decided = sum(breakdown.values())

    dry_run = bool(result.get("dry_run"))
    expected_loaded = sum(breakdown[field] for field in WRITTEN_FIELDS)
    rows_loaded = None if dry_run else int(result.get("rows_loaded") or 0)
    unwritten = None if dry_run else expected_loaded - rows_loaded

    return {
        "source_rows": source_rows,
        "decided": decided,
        "undecided": source_rows - decided,
        "rows_loaded": rows_loaded,
        "expected_loaded": None if dry_run else expected_loaded,
        "unwritten": unwritten,
        "complete": decided == source_rows and not unwritten,
        "dry_run": dry_run,
        "breakdown": breakdown,
    }


#: Where a merge that died part-way leaves its running totals, on the exception
#: on its way out.
PARTIAL_ATTR = "partial_merge"


def attach_partial(exc: BaseException, partial: dict) -> None:
    """Record how far a merge got, on the exception that stopped it.

    A merge's batches commit independently, so a failure part-way leaves the
    earlier ones applied. The exception says what went wrong and cannot say
    how much landed — and "how much landed" is the whole of what someone
    needs in order to decide whether to re-run or to go looking.

    Attached rather than raised as a new exception type so nothing about the
    original failure is lost or reclassified: the caller still sees the 413,
    the indeterminate write, or the ``KeyboardInterrupt`` it would have seen,
    and the totals ride along for whoever renders them.

    Never overwrites an existing attachment. The proxy's batch loop wraps a
    call that may already have attached its own, and the innermost is the one
    that knows how far the merge really got.
    """
    if getattr(exc, PARTIAL_ATTR, None) is None:
        try:
            setattr(exc, PARTIAL_ATTR, partial)
        except AttributeError:
            # A built-in exception with no `__dict__` cannot carry it. Losing
            # the report is not worth losing the failure it belongs to.
            pass


def partial_of(exc: BaseException) -> dict | None:
    """The totals :func:`attach_partial` left on ``exc``, if any."""
    partial = getattr(exc, PARTIAL_ATTR, None)
    return partial if isinstance(partial, dict) else None
