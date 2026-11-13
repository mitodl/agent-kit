"""Turning a classifier's raw output into a verdict, failing closed.

Ported from qm's ``parseSecurityScreenVerdict``, with one deliberate change:
qm resolves a malformed verdict to its middle posture and flags it unscreened,
while here every malformed case resolves to ``flagged``, the restrictive
verdict. Empty output, output with no JSON object, an object with no
``decision``, a non-string ``decision`` and any decision other than the two in
the output contract all land there, so a model that rambles, truncates, or is
talked into inventing a permissive value cannot produce ``clean``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

Decision = Literal["clean", "flagged"]

MALFORMED_REASON = "invalid classifier verdict"
OVERSIZE_REASON = "too long to screen"
#: The reasons witan writes itself. Only these reach an agent: a classifier's
#: own reason is free-form model output that the record being screened can
#: steer, so it stays on the row for operators (:mod:`.label`).
HOST_REASONS = frozenset({MALFORMED_REASON, OVERSIZE_REASON})
REASON_MAX_CHARS = 160
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class Verdict(BaseModel):
    """One classification of a record's text."""

    model_config = ConfigDict(frozen=True)

    decision: Decision
    reason: str | None = None
    malformed: bool = False
    """True when the classifier's output could not be read, so ``flagged``
    reflects the parser failing closed rather than the classifier's call."""


def _malformed() -> Verdict:
    return Verdict(decision="flagged", reason=MALFORMED_REASON, malformed=True)


def _first_json_object(text: str) -> object:
    """The JSON value starting at the first ``{`` in ``text``, or ``None``.

    Only the first brace is tried. Falling through to a later one would let
    text after an unparseable opening pick the verdict."""
    start = text.find("{")
    if start == -1:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return None
    return value


def _clean_reason(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    reason = _CONTROL_CHARS.sub(" ", value).strip()[:REASON_MAX_CHARS]
    return reason or None


def parse_verdict(output: str | None) -> Verdict:
    """Read a classifier's raw output as a :class:`Verdict`.

    :param output: The model's raw text.
    :returns: ``clean`` only for an object whose ``decision`` is exactly
        ``"clean"``; ``flagged`` for everything else.
    :rtype: Verdict
    """
    if output is None or not output.strip():
        return _malformed()
    parsed = _first_json_object(output)
    if not isinstance(parsed, dict):
        return _malformed()
    decision = parsed.get("decision")
    if decision == "clean":
        return Verdict(decision="clean")
    if decision == "flagged":
        return Verdict(decision="flagged", reason=_clean_reason(parsed.get("reason")))
    return _malformed()


def combine(verdicts: Iterable[Verdict]) -> Verdict:
    """One verdict for a record screened in several chunks: the first
    ``flagged`` one, else ``clean``. No verdicts is malformed, not clean.

    Stops at the first flag, so given a generator it makes no further
    classifier calls, and a later call that raises cannot turn an established
    flag into "unscreened"."""
    first = None
    for verdict in verdicts:
        if verdict.decision == "flagged":
            return verdict
        first = first or verdict
    return first or _malformed()
