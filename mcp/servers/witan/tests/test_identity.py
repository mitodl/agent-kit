"""Unit tests for witan.identity (ADR 0004) — no Keycloak/omnigraph binary required."""

import json
import threading

import pytest

import witan.identity as identity_module
from witan.identity import ActorTokenResolver, derive_actor_id


# ── derive_actor_id ───────────────────────────────────────────────────────────


def test_derive_actor_id_uuid_sub():
    assert (
        derive_actor_id("f47ac10b-58cc-4372-a567-0e02b2c3d479")
        == "act-f47ac10b-58cc-4372-a567-0e02b2c3d479"
    )


def test_derive_actor_id_lowercases():
    assert derive_actor_id("ABC123") == "act-abc123"


def test_derive_actor_id_collapses_invalid_chars():
    assert derive_actor_id("alice@example.com") == "act-alice-example-com"


def test_derive_actor_id_strips_leading_trailing_punctuation():
    assert derive_actor_id("  --alice--  ") == "act-alice"


def test_derive_actor_id_empty_raises():
    with pytest.raises(ValueError, match="Cannot derive an actor id"):
        derive_actor_id("")


def test_derive_actor_id_all_punctuation_raises():
    with pytest.raises(ValueError, match="Cannot derive an actor id"):
        derive_actor_id("!!!")


def test_derive_actor_id_non_string_raises():
    with pytest.raises(ValueError, match="sub must be a string"):
        derive_actor_id(12345)


# ── ActorTokenResolver ────────────────────────────────────────────────────────


def test_resolve_known_actor(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"act-alice": "tok-alice"}))
    resolver = ActorTokenResolver(path)
    assert resolver.resolve("act-alice") == "tok-alice"


def test_resolve_unknown_actor_raises(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"act-alice": "tok-alice"}))
    resolver = ActorTokenResolver(path)
    with pytest.raises(LookupError, match="act-bob"):
        resolver.resolve("act-bob")


def test_resolve_missing_file_raises(tmp_path):
    resolver = ActorTokenResolver(tmp_path / "does-not-exist.json")
    with pytest.raises(ValueError, match="not found"):
        resolver.resolve("act-alice")


def test_resolve_malformed_json_raises(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("not json")
    resolver = ActorTokenResolver(path)
    with pytest.raises(ValueError, match="Failed to parse"):
        resolver.resolve("act-alice")


def test_resolve_non_string_values_raises(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"act-alice": 123}))
    resolver = ActorTokenResolver(path)
    with pytest.raises(ValueError, match="JSON object"):
        resolver.resolve("act-alice")


def test_resolve_reloads_on_cache_miss_for_newly_provisioned_actor(tmp_path):
    """A user provisioned after the resolver first loaded still resolves —
    the file is re-read on a cache miss, not just once at construction."""
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"act-alice": "tok-alice"}))
    resolver = ActorTokenResolver(path)
    assert resolver.resolve("act-alice") == "tok-alice"

    path.write_text(json.dumps({"act-alice": "tok-alice", "act-bob": "tok-bob"}))
    assert resolver.resolve("act-bob") == "tok-bob"


def test_resolve_caches_and_does_not_reread_on_hit(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"act-alice": "tok-alice"}))
    resolver = ActorTokenResolver(path)
    assert resolver.resolve("act-alice") == "tok-alice"

    path.unlink()
    # Cache hit for an already-resolved actor must not touch the filesystem.
    assert resolver.resolve("act-alice") == "tok-alice"


def test_resolve_skips_reparse_when_file_unchanged(tmp_path, monkeypatch):
    """Repeated misses for an unprovisioned actor cost a cheap stat() each
    time, not a repeated read-and-parse — the mtime/size haven't changed."""
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"act-alice": "tok-alice"}))
    resolver = ActorTokenResolver(path)

    call_count = 0
    real_loads = identity_module.json.loads

    def counting_loads(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(identity_module.json, "loads", counting_loads)

    for _ in range(5):
        with pytest.raises(LookupError):
            resolver.resolve("act-bob")
    assert call_count == 1


def test_resolve_is_thread_safe(tmp_path):
    """Concurrent resolves (FastMCP runs sync tool handlers in a thread pool)
    don't race or deadlock and every thread gets the correct token."""
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"act-alice": "tok-alice", "act-bob": "tok-bob"}))
    resolver = ActorTokenResolver(path)

    results: list[str] = []
    errors: list[Exception] = []

    def worker(actor_id: str) -> None:
        try:
            results.append(resolver.resolve(actor_id))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    actor_ids = (["act-alice", "act-bob"] * 20)[:40]
    threads = [threading.Thread(target=worker, args=(a,)) for a in actor_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert sorted(results) == sorted(
        "tok-alice" if a == "act-alice" else "tok-bob" for a in actor_ids
    )
