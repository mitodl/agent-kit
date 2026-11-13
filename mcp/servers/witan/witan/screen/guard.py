"""Screening record text at write time, at ADR 0001's ``change()`` choke point.

A :class:`Screener` is a write guard like ``scan.WriteGuard`` and runs after
it, so it classifies the text that will actually be stored (after any
redaction). For a write of a screened record it adds four fields to the
params: the verdict, its reason, the classifier version, and a digest of the
text the verdict covers. Reads compare that digest with the record's current
text, so a write path that bypasses this guard (a merge, a load, a client built
without it) cannot leave an old verdict standing over new text.

Every caller of a screened mutation passes the four fields as
:data:`UNSCREENED` (all ``None``), because a batched write requires every
declared param (``witan_core.omnigraph.compose_batch``). The Screener replaces
them when it has a verdict. With no classifier configured there is no Screener,
so every write stores "unscreened", and so does a write whose classifier
raised. Both read back with the "[NOT security-screened" marker.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable

from witan_core.observability import get_logger

from ..config import ScreenConfig
from . import prompt
from .classifier import Classifier, load_classifier
from .verdict import OVERSIZE_REASON, Verdict, combine, parse_verdict

logger = get_logger("witan.screen")

Guard = Callable[[str, dict], dict]

#: node type -> (field, source label) for the text a verdict covers.
SCREENED_TEXT: dict[str, tuple[tuple[str, str], ...]] = {
    "Memory": (("title", prompt.MEMORY_TITLE), ("content", prompt.MEMORY)),
    "Task": (
        ("title", prompt.TASK),
        ("description", prompt.TASK),
        ("resolution", prompt.TASK),
    ),
    "TaskComment": (("body", prompt.TASK_COMMENT),),
}

#: The mutations that write a screened record. Each declares every
#: :data:`SCREEN_FIELDS` param (queries/mutations.gq).
SCREENED_QUERIES: dict[str, str] = {
    "insert_memory": "Memory",
    "update_memory": "Memory",
    "insert_task": "Task",
    "update_task": "Task",
    "insert_task_comment": "TaskComment",
}

SCREEN_FIELDS = ("screen_verdict", "screen_reason", "screen_version", "screen_digest")
UNSCREENED: dict[str, None] = dict.fromkeys(SCREEN_FIELDS)

MAX_PAYLOAD_CHARS = 16_000
CHUNK_CHARS = 7_500
CHUNK_OVERLAP = 500
#: The most classifier calls one write may cost (about 110,000 characters of
#: plain text). Text needing more is stored flagged without a call, so one
#: huge record can neither hold a worker for minutes nor run up the provider's
#: bill.
MAX_PAYLOADS = 16
_CACHE_SIZE = 512


def text_digest(node_type: str, record: dict) -> str:
    """sha256 of the screened text fields of ``record``, as stored or written."""
    text = [[field, record.get(field) or ""] for field, _ in SCREENED_TEXT[node_type]]
    return hashlib.sha256(json.dumps(text).encode()).hexdigest()


def _parts(node_type: str, params: dict) -> list[dict]:
    author = params.get("author")
    parts = []
    for field, source in SCREENED_TEXT[node_type]:
        content = params.get(field)
        if content:
            part = {"source": source, "content": content}
            if author:
                part["author"] = author
            parts.append(part)
    return parts


def _dumps(parts: list[dict]) -> str:
    return json.dumps(parts, ensure_ascii=False)


def _bounded(part: dict) -> list[str]:
    """``part`` as payloads under the cap, halving with overlap until each fits.

    Needed on top of the fixed-size windows because escaping can make a
    window's JSON several times longer than its text."""
    serialized = _dumps([part])
    content = part["content"]
    if len(serialized) <= MAX_PAYLOAD_CHARS or len(content) <= 1:
        return [serialized]
    mid = -(-len(content) // 2)
    overlap = min(CHUNK_OVERLAP, len(content) // 4)
    return _bounded({**part, "content": content[: mid + overlap]}) + _bounded(
        {**part, "content": content[mid - overlap :]}
    )


def _chunks(part: dict) -> list[str]:
    content = part["content"]
    step = CHUNK_CHARS - CHUNK_OVERLAP
    return [
        payload
        for start in range(0, max(len(content) - CHUNK_OVERLAP, 1), step)
        for payload in _bounded(
            {**part, "content": content[start : start + CHUNK_CHARS]}
        )
    ]


def payloads(parts: list[dict]) -> list[str]:
    """Serialize ``parts`` into payloads of at most :data:`MAX_PAYLOAD_CHARS`.

    Whole parts are packed together while they fit. A part too long to fit on
    its own is split into overlapping chunks, so an instruction straddling a
    boundary still appears whole in one of them. Nothing is dropped: a long
    record costs more classifier calls, never less coverage, up to
    :data:`MAX_PAYLOADS`, past which the Screener flags it unread.
    """
    out: list[str] = []
    group: list[dict] = []
    for part in parts:
        if len(_dumps([part])) > MAX_PAYLOAD_CHARS:
            out.extend(_chunks(part))
        elif len(_dumps([*group, part])) > MAX_PAYLOAD_CHARS:
            out.append(_dumps(group))
            group = [part]
        else:
            group.append(part)
    if group:
        out.append(_dumps(group))
    return out


class Screener:
    """Classifies a screened record's text and records the verdict on the write.

    Verdicts are cached in process by the classifier payload (text, source
    labels and author), because a task's title and description ride along on
    every claim, release and status change, and a CAS retry repeats the same
    write. The key is not :func:`text_digest`, which covers the text alone: the
    classifier also sees the author, so the same text from another author is a
    different input. The cache only saves classifier calls; the verdict of
    record is the one stored on the row, with the text digest.
    """

    def __init__(self, classifier: Classifier) -> None:
        self._classifier = classifier
        self.version = f"{classifier.version}+prompt.{prompt.PROMPT_VERSION}"
        self._cache: dict[str, Verdict] = {}
        self._cache_lock = threading.Lock()

    def __call__(self, query_name: str, params: dict) -> dict:
        node_type = SCREENED_QUERIES.get(query_name)
        if node_type is None:
            return params
        parts = _parts(node_type, params)
        key = hashlib.sha256(_dumps(parts).encode()).hexdigest()
        verdict = self._cache.get(key)
        if verdict is None:
            try:
                verdict = self._classify(parts)
            except Exception:  # a provider's failure must not fail the write
                logger.warning(
                    "witan.screen.classifier_failed",
                    query_name=query_name,
                    slug=params.get("slug"),
                    classifier=self.version,
                    exc_info=True,
                )
                return {**params, **UNSCREENED}
            logger.info(
                "witan.screen.verdict",
                query_name=query_name,
                slug=params.get("slug"),
                decision=verdict.decision,
                reason=verdict.reason,
                malformed=verdict.malformed,
                classifier=self.version,
            )
            # Tools write from worker threads (`_offload`).
            with self._cache_lock:
                if len(self._cache) >= _CACHE_SIZE:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = verdict
        return {
            **params,
            "screen_verdict": verdict.decision,
            "screen_reason": verdict.reason,
            "screen_version": self.version,
            "screen_digest": text_digest(node_type, params),
        }

    def _classify(self, parts: list[dict]) -> Verdict:
        if not parts:
            return Verdict(decision="clean")
        chunks = payloads(parts)
        if len(chunks) > MAX_PAYLOADS:
            return Verdict(decision="flagged", reason=OVERSIZE_REASON)
        return combine(
            parse_verdict(self._classifier.classify(prompt.SYSTEM_PROMPT, payload))
            for payload in chunks
        )


def screener_from_config(config: ScreenConfig) -> Screener | None:
    """The Screener for the configured classifier, or ``None`` without one."""
    classifier = load_classifier(config)
    return Screener(classifier) if classifier is not None else None


def compose_guards(*guards: Guard | None) -> Guard | None:
    """Chain write guards left to right, skipping ``None``; ``None`` if all are."""
    active = [g for g in guards if g is not None]
    if not active:
        return None
    if len(active) == 1:
        return active[0]

    def chained(query_name: str, params: dict) -> dict:
        for guard in active:
            params = guard(query_name, params)
        return params

    return chained
