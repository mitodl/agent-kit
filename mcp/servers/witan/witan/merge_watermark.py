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

The two marks are only ever compared against their own store, never against each
other — the source is a laptop's clock and the target is a cluster's, and a
cross-clock comparison would invent divergences out of skew alone.

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


def _load() -> list[dict]:
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict) or raw.get("version") != _VERSION:
        return []
    pairs = raw.get("pairs")
    return [p for p in pairs if isinstance(p, dict)] if isinstance(pairs, list) else []


def read(source: str, target: str) -> dict | None:
    """The watermark from the last merge of ``source`` into ``target``, or None.

    None is the ordinary answer for a first merge, and a caller must read it as
    "cannot tell", never as "nothing diverged"."""
    for entry in _load():
        if entry.get("source") == source and entry.get("target") == target:
            return entry
    return None


def write(source: str, target: str, watermark: dict) -> bool:
    """Record ``watermark`` for this pair, replacing any earlier one. True if it
    landed.

    Written through a temp file in the same directory so an interrupted write
    cannot leave half a file behind — a corrupt watermark reads as absent, which
    silently disables the divergence report for that pair until the next
    successful merge rewrites it."""
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
