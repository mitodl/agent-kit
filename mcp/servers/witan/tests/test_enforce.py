"""Tests for write-path enforcement: WriteGuard block/redact/warn + graph wiring."""

import pytest

from witan.config import ScanConfig
from witan.graph import OmnigraphClient
from witan.scan import (
    Finding,
    ScannerRegistry,
    WriteBlocked,
    WriteGuard,
    masked_preview,
    write_guard_from_config,
)


class MatchScanner:
    """Flags every occurrence of ``needle``; category/action configurable."""

    def __init__(self, name, category, needle, *, action=None):
        self.name = name
        self.category = category
        self._needle = needle
        self._action = action

    def scan(self, text, field, node_type):
        out = []
        start = text.find(self._needle)
        while start != -1:
            end = start + len(self._needle)
            out.append(
                Finding(
                    detector=self.name,
                    category=self.category,
                    start=start,
                    end=end,
                    preview=masked_preview(self.name, self._needle),
                    action=self._action,
                )
            )
            start = text.find(self._needle, end)
        return out


class BoomScanner:
    name = "boom"
    category = "secret"

    def scan(self, text, field, node_type):
        raise RuntimeError("kaboom")


def _guard(scanners, **cfg_kw):
    cfg = ScanConfig(enabled=True, **cfg_kw)
    return WriteGuard(cfg, ScannerRegistry(scanners))


# ── factory gating ──────────────────────────────────────────────────────────────


def test_factory_returns_none_when_disabled():
    assert write_guard_from_config(ScanConfig(enabled=False)) is None


def test_factory_builds_guard_when_enabled():
    assert isinstance(write_guard_from_config(ScanConfig(enabled=True)), WriteGuard)


# ── mapping / scope ─────────────────────────────────────────────────────────────


def test_unmapped_query_is_untouched():
    guard = _guard([MatchScanner("s", "secret", "SECRET")])
    params = {"from": "a", "to": "SECRET"}
    assert guard("link_tagged", params) is params


def test_only_mapped_fields_are_scanned():
    """A secret in an unscanned field (repo) must pass; content is scanned."""
    guard = _guard([MatchScanner("s", "secret", "SECRET")])
    params = {"title": "ok", "content": "clean", "repo": "SECRET"}
    assert guard("insert_memory", params) == params


# ── block ───────────────────────────────────────────────────────────────────────


def test_secret_blocks_by_default():
    guard = _guard([MatchScanner("aws_key", "secret", "AKIASECRET")])
    with pytest.raises(WriteBlocked) as exc:
        guard("insert_memory", {"title": "t", "content": "here AKIASECRET x"})
    assert exc.value.query_name == "insert_memory"


def test_block_message_is_secret_free():
    guard = _guard([MatchScanner("aws_key", "secret", "AKIASECRETVALUE")])
    with pytest.raises(WriteBlocked) as exc:
        guard("insert_task", {"title": "t", "description": "d AKIASECRETVALUE"})
    msg = str(exc.value)
    assert "AKIASECRETVALUE" not in msg
    assert "aws_key" in msg
    assert "description" in msg


def test_block_wins_over_redact_across_fields():
    scanners = [
        MatchScanner("email", "pii", "a@b.com"),  # would redact
        MatchScanner("aws_key", "secret", "AKIA"),  # blocks
    ]
    guard = _guard(scanners)  # secret_action=block, pii_action=redact (defaults)
    with pytest.raises(WriteBlocked):
        guard("insert_memory", {"title": "a@b.com", "content": "AKIA"})


# ── redact ──────────────────────────────────────────────────────────────────────


def test_pii_redacts_by_default():
    guard = _guard([MatchScanner("email", "pii", "a@b.com")])
    out = guard("insert_memory", {"title": "t", "content": "mail a@b.com now"})
    assert out["content"] == "mail «redacted:email» now"
    assert "a@b.com" not in out["content"]


def test_redaction_leaves_other_params_intact():
    guard = _guard([MatchScanner("email", "pii", "a@b.com")])
    params = {"title": "t", "content": "a@b.com", "repo": "r", "author": "x"}
    out = guard("insert_memory", params)
    assert out["repo"] == "r" and out["author"] == "x"
    assert out["content"] == "«redacted:email»"


def test_redaction_merges_overlapping_and_multiple_spans():
    guard = _guard([MatchScanner("tok", "pii", "XX")])
    out = guard("insert_memory", {"title": "t", "content": "XX-XX"})
    assert out["content"] == "«redacted:tok»-«redacted:tok»"


# ── warn ────────────────────────────────────────────────────────────────────────


def test_warn_passes_content_through(caplog):
    guard = _guard([MatchScanner("aws_key", "secret", "AKIA")], secret_action="warn")
    params = {"title": "t", "content": "AKIA stays"}
    out = guard("insert_memory", params)
    assert out == params


def test_per_finding_action_overrides_category_default():
    """A finding that forces block wins even though pii defaults to redact."""
    guard = _guard([MatchScanner("ssn", "pii", "111-22-3333", action="block")])
    with pytest.raises(WriteBlocked):
        guard("insert_memory", {"title": "t", "content": "111-22-3333"})


# ── allowlist suppression ────────────────────────────────────────────────────


def test_regex_allowlisted_secret_is_not_blocked():
    guard = _guard([MatchScanner("aws_key", "secret", "AKIA")], allowlist=["AKIA"])
    params = {"title": "t", "content": "here AKIA stays"}
    assert guard("insert_memory", params) == params


def test_pragma_allowlisted_secret_is_not_blocked():
    guard = _guard([MatchScanner("aws_key", "secret", "AKIA")])
    params = {"title": "t", "content": "here AKIA witan: allow-secret"}
    assert guard("insert_memory", params) == params


def test_suppressed_pii_is_not_redacted():
    guard = _guard([MatchScanner("email", "pii", "a@b.com")], allowlist=["a@b\\.com"])
    params = {"title": "t", "content": "mail a@b.com now"}
    assert guard("insert_memory", params) == params


def test_overlay_applies_repo_specific_policy():
    """A repo with an overlay entry gets its own enforcement mode."""
    guard = _guard(
        [MatchScanner("aws_key", "secret", "AKIA")],
        overlay={"github.com/example/legacy": {"secret_action": "warn"}},
    )
    params = {
        "title": "t",
        "content": "AKIA stays",
        "repo": "github.com/example/legacy",
    }
    assert guard("insert_memory", params) == params


def test_overlay_does_not_affect_unmatched_repo():
    guard = _guard(
        [MatchScanner("aws_key", "secret", "AKIA")],
        overlay={"github.com/example/legacy": {"secret_action": "warn"}},
    )
    with pytest.raises(WriteBlocked):
        guard(
            "insert_memory",
            {"title": "t", "content": "AKIA stays", "repo": "github.com/other/repo"},
        )


def test_overlay_keys_on_first_repos_entry():
    """insert_workflow_project carries `repos` (a list), not `repo`."""
    guard = _guard(
        [MatchScanner("aws_key", "secret", "AKIA")],
        overlay={"github.com/example/legacy": {"secret_action": "warn"}},
    )
    params = {
        "title": "t",
        "description": "AKIA stays",
        "repos": ["github.com/example/legacy", "github.com/other/repo"],
    }
    assert guard("insert_workflow_project", params) == params


def test_suppression_does_not_leak_across_unrelated_findings():
    """Allowlisting one finding must not suppress a different, unrelated one
    in the same write."""
    guard = _guard(
        [
            MatchScanner("email", "pii", "a@b.com"),
            MatchScanner("aws_key", "secret", "AKIA"),
        ],
        allowlist=["a@b\\.com"],
    )
    with pytest.raises(WriteBlocked):
        guard("insert_memory", {"title": "t", "content": "a@b.com AKIA"})


# ── on_scanner_error ─────────────────────────────────────────────────────────────


def test_scanner_error_fails_closed_by_default():
    guard = _guard([BoomScanner()])  # on_scanner_error defaults to block
    with pytest.raises(RuntimeError):
        guard("insert_memory", {"title": "t", "content": "anything"})


def test_scanner_error_warn_allows_write():
    guard = _guard([BoomScanner()], on_scanner_error="warn")
    params = {"title": "t", "content": "anything"}
    assert guard("insert_memory", params) == params


# ── graph.py wiring ──────────────────────────────────────────────────────────────


def test_change_applies_guard_before_persist(monkeypatch, tmp_path):
    """change() must feed guard output (redacted params) to the mutate call."""
    guard = _guard([MatchScanner("email", "pii", "a@b.com")])
    client = OmnigraphClient(str(tmp_path / "g.omni"), tmp_path, guard=guard)

    captured = {}

    def fake_run(subcommand, *args, **kwargs):
        captured["args"] = args
        return ""

    monkeypatch.setattr(client, "_run", fake_run)
    client.change("mutations.gq", "insert_memory", {"title": "t", "content": "a@b.com"})

    params_json = captured["args"][captured["args"].index("--params") + 1]
    assert "a@b.com" not in params_json
    assert "redacted:email" in params_json  # guillemets are JSON-escaped to «


def test_change_guard_block_prevents_persist(monkeypatch, tmp_path):
    guard = _guard([MatchScanner("aws_key", "secret", "AKIA")])
    client = OmnigraphClient(str(tmp_path / "g.omni"), tmp_path, guard=guard)

    called = False

    def fake_run(subcommand, *args, **kwargs):
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(client, "_run", fake_run)
    with pytest.raises(WriteBlocked):
        client.change(
            "mutations.gq", "insert_memory", {"title": "t", "content": "AKIA"}
        )
    assert called is False


# ── TaskComment coverage ────────────────────────────────────────────────────────


def test_task_comment_body_is_scanned():
    """`task_comment` is a free-text write like any other, so it is subject to
    the same block/redact/warn policy — a body carrying a secret is refused."""
    guard = _guard([MatchScanner("aws_key", "secret", "AKIA")])
    with pytest.raises(WriteBlocked):
        guard("insert_task_comment", {"body": "the key is AKIA", "slug": "tc-1"})


def test_task_comment_body_is_redacted():
    guard = _guard([MatchScanner("email", "pii", "a@b.com")])
    out = guard("insert_task_comment", {"body": "ping a@b.com", "slug": "tc-1"})
    assert "a@b.com" not in out["body"]


def test_task_comment_overlay_keys_on_the_tasks_repo():
    """A TaskComment has no repo of its own; `task_comment` passes its task's so
    a `[scan.overlay]` table still reaches it."""
    guard = _guard(
        [MatchScanner("aws_key", "secret", "AKIA")],
        overlay={"github.com/example/legacy": {"secret_action": "warn"}},
    )
    params = {
        "body": "AKIA stays",
        "slug": "tc-1",
        "repo": "github.com/example/legacy",
    }
    assert guard("insert_task_comment", params) == params
