"""Where a merge records what both stores looked like when they last agreed.

``witan migrate merge`` reconciles newest-record-wins per node, and a
record-level timestamp cannot tell "the target is simply ahead" from "both sides
advanced independently since the last merge". The second is a divergence — one
side's edit is about to be discarded — and without something written down at
merge time it is indistinguishable from the first (see
tk-witan-migrate-merge-silently-drops-divergent-edi-1f1453).

So each merge stores a **per-side high-water mark**: the newest comparison
timestamp present in the source, and the newest that will be in the target once
this merge's winners land. Next merge, a node whose source timestamp is past the
source mark *and* whose target timestamp is past the target mark was written on
both sides, and is reported instead of silently resolved.

The two marks are compared against their own store, not against each other — the
source is a laptop's clock and the target is a cluster's, and a cross-clock
comparison would invent divergences out of skew alone. One documented exception,
with its cost stated, lives in ``witan.server._next_watermark``: the rows a merge
loads carry source timestamps into the target, so the target mark is raised to
cover them, which leaves a blind window the width of any source-ahead skew.

Client-side state, deliberately. The pairing is "this store, that deployment",
which only the machine holding the source knows; the deployment sees a batch of
rows and cannot say which store they came from.

Everything here fails soft. No file, an unreadable one, a truncated one: all mean
"no watermark", which costs the divergence report and nothing else — a merge must
not fail because a hint file is missing.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path.home() / ".config" / "witan" / "merge-watermarks.json"
"""Where merge watermarks are kept, overridable with ``WITAN_MERGE_WATERMARKS``.

Alongside ``tokens.json`` (``witan_core.remote.oidc.DEFAULT_CACHE_PATH``) rather
than inside the store: the store is the thing being merged, and a watermark that
travelled with it would be restored by the very export/load cycle it exists to
describe."""

_VERSION = 1


def path() -> Path:
    override = os.environ.get("WITAN_MERGE_WATERMARKS")
    return Path(override).expanduser() if override else DEFAULT_PATH


def canonical(uri: str) -> str:
    """The spelling a store is keyed under, so two names for it share a mark.

    A pair is keyed by what the caller typed, and a caller types a store many
    ways: `graph.omni` from two different working directories is two stores
    under one key, while `/tmp/graph.omni`, `../tmp/graph.omni` and
    `file:///tmp/graph.omni` are one store under three. Either way the mark
    describes a graph that is not the one being merged, which is worse than
    having no mark at all — it reports divergence against someone else's
    history.

    Local paths resolve to an absolute real path (symlinks included, since a
    store reached through a symlink is the same store). Remote URIs are left
    alone apart from a trailing slash: their authority and path are already
    canonical, and rewriting them risks changing which graph they name.
    """
    if uri.startswith("file://"):
        uri = uri[len("file://") :]
    elif uri.startswith(("http://", "https://", "s3://")):
        return uri.rstrip("/")
    return str(Path(uri).expanduser().resolve())


def is_usable(watermark: dict | None) -> bool:
    """Whether a mark can actually answer the divergence question.

    A mark missing either side is indistinguishable from no mark at all —
    `_reconcile_nodes` parses the absent side to `None` and flags nothing — but
    it is *truthy*, so storing one suppresses the "no watermark, cannot tell"
    notice while detecting nothing. That combination is the one outcome this
    feature exists to prevent: silence that reads as "nothing diverged".
    """
    return bool(watermark) and all(
        watermark.get(side) is not None for side in ("source_ts", "target_ts")
    )


def _load() -> list[dict]:
    # ValueError, not just JSONDecodeError: a corrupt file need not be valid
    # UTF-8, and `read_text` raises UnicodeDecodeError before `json.loads` is
    # ever reached. It is a ValueError but not an OSError, so it escaped the
    # narrower tuple and took the whole merge down from a module documented to
    # fail soft.
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict) or raw.get("version") != _VERSION:
        return []
    pairs = raw.get("pairs")
    return [p for p in pairs if isinstance(p, dict)] if isinstance(pairs, list) else []


def read(source: str, target: str) -> dict | None:
    """The watermark from the last merge of ``source`` into ``target``, or None.

    None is the ordinary answer for a first merge, and a caller must read it as
    "cannot tell", never as "nothing diverged". A stored mark that cannot answer
    the question (see :func:`is_usable`) is reported as absent for the same
    reason."""
    source, target = canonical(source), canonical(target)
    for entry in _load():
        if entry.get("source") == source and entry.get("target") == target:
            return entry if is_usable(entry) else None
    return None


def forget(source: str, target: str) -> bool:
    """Drop this pair's mark. True if the file is left without one.

    Called before a merge writes anything, because a merge is not atomic: its
    batches commit independently, so a run that dies part-way leaves rows in the
    target that the standing mark predates. The next run then reads those rows
    as an independent target edit and reports divergence against a row nothing
    but the failed merge ever wrote.

    Dropping the mark first turns that into "cannot tell", which is the honest
    answer for a graph whose last write was a partial merge. A successful run
    puts a fresh mark back immediately.
    """
    source, target = canonical(source), canonical(target)
    pairs = [
        p
        for p in _load()
        if not (p.get("source") == source and p.get("target") == target)
    ]
    return _store(pairs)


def write(source: str, target: str, watermark: dict) -> bool:
    """Record ``watermark`` for this pair, replacing any earlier one. True if it
    landed.

    Refuses a mark :func:`is_usable` rejects, rather than storing something that
    reads as "we have a watermark" and answers nothing."""
    if not is_usable(watermark):
        return False
    source, target = canonical(source), canonical(target)
    entry = {
        "source": source,
        "target": target,
        "source_ts": watermark.get("source_ts"),
        "target_ts": watermark.get("target_ts"),
        "merged_at": datetime.now(timezone.utc).isoformat(),
    }
    pairs = [
        p
        for p in _load()
        if not (p.get("source") == source and p.get("target") == target)
    ]
    pairs.append(entry)
    return _store(pairs)


def _store(pairs: list[dict]) -> bool:
    """Replace the file with ``pairs``. True if it landed.

    Written through a temp file in the same directory so an interrupted write
    cannot leave half a file behind — a corrupt watermark reads as absent, which
    silently disables the divergence report for that pair until the next
    successful merge rewrites it."""
    destination = path()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=destination.name + ".",
            delete=False,
        ) as handle:
            json.dump({"version": _VERSION, "pairs": pairs}, handle, indent=2)
            temp_name = handle.name
        os.replace(temp_name, destination)
    except OSError:
        return False
    return True
