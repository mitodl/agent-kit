"""Tests for ``derive_actor_handle`` — the log-safe half of caller identity.

``derive_actor_id`` is covered by the witan server's own test_identity.py; this
file is about the *handle*, whose whole reason to exist is what it leaves out.
"""

from witan_core.identity import derive_actor_handle


def test_prefers_preferred_username():
    claims = {"preferred_username": "tmacey@mit.edu", "email": "other@mit.edu"}
    assert derive_actor_handle(claims) == "tmacey"


def test_the_domain_never_survives():
    # The point of the whole function. `preferred_username` is a full email in
    # this realm and witan's own scanner calls a bare email `pii`; these lines
    # go to Loki. A regression that starts logging the address again must fail
    # here rather than in a log-retention review months later.
    handle = derive_actor_handle({"preferred_username": "tmacey@mit.edu"})
    assert "@" not in handle
    assert "mit.edu" not in handle


def test_falls_back_to_email():
    assert derive_actor_handle({"email": "someone@mit.edu"}) == "someone"


def test_a_bare_username_is_returned_unchanged():
    # A realm that stops putting emails in this claim keeps working untouched.
    assert derive_actor_handle({"preferred_username": "tmacey"}) == "tmacey"


def test_no_usable_claim_is_none_not_empty_string():
    # None means "omit the field"; "" would log a blank one, which reads as a
    # bug in the logging rather than an absence of identity.
    assert derive_actor_handle({}) is None
    assert derive_actor_handle({"sub": "36615884-fc52"}) is None


def test_non_string_and_blank_claims_are_skipped():
    assert derive_actor_handle({"preferred_username": None, "email": "a@b.c"}) == "a"
    assert derive_actor_handle({"preferred_username": 12345, "email": "a@b.c"}) == "a"
    assert derive_actor_handle({"preferred_username": "   "}) is None
    assert derive_actor_handle({"preferred_username": "@mit.edu"}) is None
