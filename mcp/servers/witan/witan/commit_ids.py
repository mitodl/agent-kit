"""Ordering of omnigraph graph commit ids.

Through 0.11 every ``graph_commit_id`` is a bare ULID, so string comparison
orders them by creation time. From 0.12 only a graph's bootstrap commit (and
the commits a store had before ``omnigraph upgrade``) is a bare ULID. Every
later one is a history-block id::

    hb1.<block>.<slot>.<nonce>

``block`` and ``nonce`` are ULIDs and ``slot`` is a decimal integer
(upstream ``omnigraph-core`` ``graph_commit_id.rs``). Compared as strings,
``hb1.X.9.…`` sorts after ``hb1.X.10.…``, so a newer commit reads as older
from the tenth write in a block on.

A read's envelope carries the id and nothing else (no
``graph_manifest_version``), so the id is all there is to order on.
"""

from __future__ import annotations

import re
from typing import NamedTuple

_HISTORY_BLOCK_PREFIX = "hb1."
# Canonical 26-character Crockford base32. The first character holds only
# three bits, so it stops at 7.
_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_ULID_RE = re.compile(_ULID)
# Upstream's `HISTORY_BLOCK_SLOTS` (16 * 1024). Its parser refuses a slot at
# or above this, so an id carrying one did not come from omnigraph.
_HISTORY_BLOCK_SLOTS = 16384
_HISTORY_BLOCK_RE = re.compile(
    rf"hb1\.(?P<block>{_ULID})\.(?P<slot>0|[1-9][0-9]*)\.(?P<nonce>{_ULID})"
)


class _CommitId(NamedTuple):
    #: The history block, or ``None`` for a bare ULID.
    block: str | None
    slot: int
    #: The ULID the writer minted for this commit. A bare id is its own nonce.
    nonce: str


def _parse(commit_id: str) -> _CommitId:
    if commit_id.startswith(_HISTORY_BLOCK_PREFIX):
        match = _HISTORY_BLOCK_RE.fullmatch(commit_id)
        if match is None or int(match["slot"]) >= _HISTORY_BLOCK_SLOTS:
            raise ValueError(f"malformed history-block commit id: {commit_id!r}")
        return _CommitId(match["block"], int(match["slot"]), match["nonce"])
    if _ULID_RE.fullmatch(commit_id) is None:
        raise ValueError(f"unrecognised graph commit id: {commit_id!r}")
    return _CommitId(None, 0, commit_id)


def commit_reached(seen: str, floor: str) -> bool:
    """Whether a read at ``seen`` is at least as new as the commit ``floor``.

    Both ids must come from the same branch.

    - Two commits of one history block order by slot. That is exact: the
      publisher gives a commit its parent's block only when its slot is the
      parent's plus one.
    - A bare ULID is older than any history-block id. It is the bootstrap
      commit or predates the store's upgrade.
    - Anything else (two bare ULIDs, or two blocks) orders by the commits' own
      nonces. A nonce is a ULID the writer mints from its clock, so this is
      as good as the writers' clocks, which is the assumption comparing
      0.11's ids as strings already made.

    :param seen: The ``graph_commit_id`` a read reported.
    :param floor: The ``graph_commit_id`` a write produced.
    :returns: ``True`` if ``seen`` is ``floor`` or a later commit.
    :rtype: bool
    :raises ValueError: If either id is neither shape. An id we cannot place
        must not be compared as if we could.
    """
    if seen == floor:
        return True
    seen_id, floor_id = _parse(seen), _parse(floor)
    if seen_id.block is not None and seen_id.block == floor_id.block:
        return seen_id.slot >= floor_id.slot
    if (seen_id.block is None) != (floor_id.block is None):
        return floor_id.block is None
    return seen_id.nonce >= floor_id.nonce
