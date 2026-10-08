import itertools

import pytest

from witan.commit_ids import commit_reached

from .conftest import requires_omnigraph

_BOOTSTRAP = "01M4E8BHWJXYRAPPYBJXDD4CWM"
_BLOCK = "01M4E8BJQ4NRAGQJE0B36BDV5N"
_NONCE = "01M4E8BKX6DYM327NMQRDXB4B2"
_LATER_NONCE = "01M4E8BKZGE3C8Q4Y5GY9FMKXB"


def _hb(block: str, slot: int, nonce: str = _NONCE) -> str:
    return f"hb1.{block}.{slot}.{nonce}"


def test_slots_order_as_integers_not_text():
    # The regression: "…9.…" > "…10.…" as strings.
    assert _hb(_BLOCK, 9) > _hb(_BLOCK, 10)
    assert commit_reached(_hb(_BLOCK, 10), _hb(_BLOCK, 9))
    assert not commit_reached(_hb(_BLOCK, 9), _hb(_BLOCK, 10))


def test_within_a_block_the_slot_decides_not_the_nonce():
    assert commit_reached(_hb(_BLOCK, 2, _NONCE), _hb(_BLOCK, 1, _LATER_NONCE))
    assert not commit_reached(_hb(_BLOCK, 1, _LATER_NONCE), _hb(_BLOCK, 2, _NONCE))


def test_a_commit_reaches_itself():
    assert commit_reached(_hb(_BLOCK, 3), _hb(_BLOCK, 3))
    assert commit_reached(_BOOTSTRAP, _BOOTSTRAP)


def test_across_blocks_the_commits_own_nonces_decide():
    # A block is named after the commit that opened it. Ordering by that name
    # would call every commit of the second block older if its opener minted
    # its nonce first, whatever the two commits being compared say.
    earlier_block, later_block = _BLOCK, "01M4E9ZZZZZZZZZZZZZZZZZZZZ"
    older = _hb(later_block, 0, _NONCE)
    newer = _hb(earlier_block, 4000, _LATER_NONCE)

    assert commit_reached(newer, older)
    assert not commit_reached(older, newer)


def test_a_bare_ulid_is_older_than_any_history_block_id():
    # The bootstrap commit, or a commit from before `omnigraph upgrade`. The
    # shape decides, not the text.
    newer_ulid = "7ZZZZZZZZZZZZZZZZZZZZZZZZZ"
    assert commit_reached(_hb(_BLOCK, 1), newer_ulid)
    assert not commit_reached(newer_ulid, _hb(_BLOCK, 1))


def test_bare_ulids_order_as_text():
    assert commit_reached("01M4E8BHWJXYRAPPYBJXDD4CWN", _BOOTSTRAP)
    assert not commit_reached(_BOOTSTRAP, "01M4E8BHWJXYRAPPYBJXDD4CWN")


@pytest.mark.parametrize(
    "commit_id",
    [
        "",
        "01WRITE",
        "8ZZZZZZZZZZZZZZZZZZZZZZZZZ",
        "hb1",
        f"hb1.{_BLOCK}.1",
        f"hb1.{_BLOCK}.01.{_NONCE}",
        f"hb1.{_BLOCK}.-1.{_NONCE}",
        f"hb1.{_BLOCK}.1.{_NONCE}.extra",
        f"hb2.{_BLOCK}.1.{_NONCE}",
    ],
)
def test_an_id_of_unknown_shape_is_refused(commit_id):
    with pytest.raises(ValueError):
        commit_reached(commit_id, _BOOTSTRAP)
    with pytest.raises(ValueError):
        commit_reached(_BOOTSTRAP, commit_id)


@requires_omnigraph
def test_real_commit_ids_order_the_way_the_writes_happened(server):
    """Twelve writes, because the 0.12+ id carries a decimal slot and text
    order first goes wrong at the tenth. On 0.11 the ids are bare ULIDs and
    this pins the same order."""
    from witan import server as srv

    seen = []
    for i in range(12):
        task = server.task_create(title=f"order-{i}", description="x")
        _, commit = srv.client.read_with_commit(
            "read.gq", "get_task", {"slug": task["slug"]}
        )
        seen.append(commit)

    assert all(seen), "the read envelope carried no graph_commit_id"
    for earlier, later in itertools.pairwise(seen):
        assert commit_reached(later, earlier)
        assert not commit_reached(earlier, later)
