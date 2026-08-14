"""Tell the caller their content was rewritten (ADR 0001 §D3, amendment).

Redaction used to be invisible from the outside. :func:`~.redact.redact_spans`
altered the value, :func:`~.redact.flag_redacted` tagged the node, an audit
event went to the log — and the tool returned success with nothing in the
response to say the stored content is not the content that was sent. The only
way to find out was to read the row back and notice.

★ THAT IS A DATA-LOSS PATH, NOT A COSMETIC GAP. The replacement is not
reversible: the original span is kept nowhere, so by the time anybody notices,
there is nothing to restore from. It was found by losing real measurements to
it — a ``task_update`` carrying handler latencies came back with
``«redacted:credit_card»`` where the numbers had been
(tk-write-path-redaction-silently-rewrites-content-a-aec2b6).

So a redaction now leaves a trail the caller actually reads. The guard records
one :class:`RedactionNotice` per rewritten span; the write path attaches them to
the tool's result via :func:`take_redactions`.

★ THE OFFSETS ARE THE MESSAGE, AND THE MATCHED TEXT IS DELIBERATELY ABSENT.
ADR 0001 §D3 forbids putting a matched value in any error or log line, and a
tool result is a worse place than most — it is transcript, it is context, and
for the ``secret`` category it would hand back the exact thing the detector
exists to keep out of the graph. ``start``/``end`` index the value the caller
just sent, so the caller can look at their own input and judge the match in one
step without anything sensitive crossing back over the wire.

Scope: this is a report, not an override. Overriding a wrong match still means
re-sending the content in a shape the detector does not claim — see the task
above for why an elicitation-based "store it anyway" does not work today
(tk-the-cli-can-never-reach-the-server-s-steal-promp-555c64: the server's
``elicit.confirm`` never reaches a CLI user).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel, ConfigDict
from witan_core.observability import get_logger

from .models import Category, Finding

logger = get_logger("witan.scan")


class RedactionNotice(BaseModel):
    """One span that was replaced, described without quoting it."""

    model_config = ConfigDict(frozen=True)

    query_name: str
    """The mutation whose params were rewritten, e.g. ``update_task``."""

    slug: str | None
    """Which row lost content — ``params['slug']``, where the mutation has one.

    ★ WITHOUT THIS A REPORT CAN BE WORSE THAN NO REPORT. One tool call can
    rewrite many rows: ``migrate_repo_keys`` walks every task and memory in the
    graph. Two rows whose match lands in the same field at the same offsets are
    indistinguishable without the slug, so the dedupe below would drop one as a
    duplicate — telling the caller one row was altered when two were, and not
    saying which.
    """

    field: str
    detector: str
    category: Category
    start: int
    end: int
    """Half-open offsets into the value **as the caller sent it**."""

    @property
    def key(self) -> tuple:
        return (
            self.query_name,
            self.slug,
            self.field,
            self.detector,
            self.start,
            self.end,
        )


# The notices recorded so far on this call.
#
# ★ A TUPLE, REPLACED ON EVERY RECORD, NEVER A MUTABLE LIST. A ``ContextVar``
# is copied — not shared — into a worker thread or a child task, so a `.set()`
# there cannot leak back into the context that spawned it. That is what makes a
# notice belong to exactly the call that produced it, with no cross-request
# bleed on a deployed replica serving many callers at once. A mutable list would
# be shared by reference and give up precisely that property.
#
# The flip side, and the constraint on every wiring site: ``record`` and
# ``annotate`` must run in the SAME context, or the notice is invisible to the
# reader. They do today — FastMCP runs a sync tool wholly in one worker thread,
# and witan's async tools call their sync write helpers inline on the event
# loop, so the write and the result-building are always on the same side.
_recorded: ContextVar[tuple[RedactionNotice, ...]] = ContextVar(
    "witan_scan_redactions", default=()
)


def record(
    query_name: str, slug: str | None, field: str, findings: list[Finding]
) -> None:
    """Note that ``findings`` were redacted out of ``field`` of row ``slug``.

    Deduplicated on the full span identity — INCLUDING the slug — because one
    tool call legitimately issues the same write twice: ``_store_memory``
    retries its batch without the provenance edge when the edge endpoint is
    stale, and reporting that as two redactions would misdescribe one rewrite as
    two. Dedupe must separate a genuine retry of ONE row from the same span in a
    DIFFERENT row, which is why the slug is part of the key and not merely
    reported alongside it.
    """
    existing = _recorded.get()
    seen = {n.key for n in existing}
    added = []
    for f in findings:
        notice = RedactionNotice(
            query_name=query_name,
            slug=slug,
            field=field,
            detector=f.detector,
            category=f.category,
            start=f.start,
            end=f.end,
        )
        if notice.key not in seen:
            seen.add(notice.key)
            added.append(notice)
    if added:
        _recorded.set((*existing, *added))


def take_redactions() -> tuple[RedactionNotice, ...]:
    """Everything recorded since the last take, clearing it.

    Clearing is what keeps a notice attached to the write that caused it. A
    tool that reports its redactions and leaves them in place would have the
    next tool in the same context report them too.
    """
    notices = _recorded.get()
    if notices:
        _recorded.set(())
    return notices


@contextmanager
def no_redactions() -> Iterator[None]:
    """Isolate a block's recorded notices from the surrounding call. For tests."""
    token = _recorded.set(())
    try:
        yield
    finally:
        _recorded.reset(token)


def describe(notices: tuple[RedactionNotice, ...]) -> str:
    """A one-line, secret-free sentence a caller can act on."""
    spans = ", ".join(
        f"{n.slug + '.' if n.slug else ''}{n.field}[{n.start}:{n.end}] "
        f"matched {n.detector}"
        for n in notices
    )
    return (
        f"⚠ CONTENT WAS ALTERED BEFORE STORAGE: {spans}. "
        "Each span was replaced by «redacted:<detector>» and the original is not "
        "recoverable. If this was a false positive, re-send the content in a "
        "shape the detector does not match — separating long digit runs with "
        "commas or units defeats the card-number rule — and correct the stored "
        "value."
    )


def annotate(result: Any) -> Any:
    """Attach this call's redaction notices to a tool result, if there are any.

    Applied to EVERY tool (see ``witan.server._tool``), so most calls pass
    through untouched — a read records nothing, and a clean write records
    nothing. Only a call that actually rewrote content grows keys.

    Anything that is not a ``dict`` is returned unchanged: a listing, a scalar,
    or the ``None`` that means "no such row" has nowhere to carry the report.
    That case is LOGGED rather than dropped quietly, because it means a write
    altered content and the caller is about to be told nothing — the exact
    failure this module exists to end. There is no such tool today; the log line
    is what makes it visible if one is ever added.
    """
    notices = take_redactions()
    if not notices:
        return result
    if not isinstance(result, dict):
        logger.warning(
            "witan.scan.redaction_unreportable",
            result_type=type(result).__name__,
            redactions=[n.model_dump() for n in notices],
        )
        return result
    return {
        **result,
        "redactions": [n.model_dump() for n in notices],
        "redaction_note": describe(notices),
    }
