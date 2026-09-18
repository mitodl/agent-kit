"""End-to-end CLI coverage for staleness detection — see "Part B" of
``agent-config-kit-config-overlay-spec.md``. Complements the unit-level
tests in ``test_installers.py`` (content hashing), ``test_prune.py``
(``AppliedState``/state-file I/O), and ``test_diff.py`` (``Drift.stale_keys``)
with the actual CLI behavior a user would see.
"""

import json
from pathlib import Path

import pytest

from agent_config_kit.cli import app
from agent_config_kit.prune import default_applied_state_path


def _write_manifest(tmp_path: Path, text: str) -> Path:
    manifest = tmp_path / "agent-config.toml"
    manifest.write_text(text)
    return manifest


def _write_skill(tmp_path: Path, rel: str, body: str) -> Path:
    skill_md = tmp_path / rel
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(body)
    return skill_md


def _run_ok(args: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        app(args)
    assert exc_info.value.code == 0


def test_apply_first_run_writes_applied_state_and_prints_no_warning(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_skill(tmp_path, "skills/commit/SKILL.md", "# commit v1")
    manifest = _write_manifest(
        tmp_path,
        """
        [skills]
        commit = "skills/commit/SKILL.md"
        """,
    )

    _run_ok(["apply", str(manifest), "--platform", "claude"])

    out = capsys.readouterr().out
    assert "changed upstream" not in out
    state_path = default_applied_state_path(manifest)
    assert state_path.is_file()
    recorded = json.loads(state_path.read_text())
    assert "commit" in recorded["platforms"]["claude"]["skills"]


def test_apply_warns_when_skill_source_changed_since_last_apply(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill_md = _write_skill(tmp_path, "skills/commit/SKILL.md", "# commit v1")
    manifest = _write_manifest(
        tmp_path,
        """
        [skills]
        commit = "skills/commit/SKILL.md"
        """,
    )
    _run_ok(["apply", str(manifest), "--platform", "claude"])
    skill_md.write_text("# commit v2, changed upstream")

    _run_ok(["apply", str(manifest), "--platform", "claude"])

    out = capsys.readouterr().out
    assert "⚠ 1 entry changed upstream since last apply: commit" in out


def test_apply_never_blocks_on_staleness_still_writes_the_changed_content(
    tmp_path, monkeypatch
):
    """S4: staleness is informational only -- apply still proceeds and
    re-copies the changed content, exit code 0."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill_md = _write_skill(tmp_path, "skills/commit/SKILL.md", "# commit v1")
    manifest = _write_manifest(
        tmp_path,
        """
        [skills]
        commit = "skills/commit/SKILL.md"
        """,
    )
    _run_ok(["apply", str(manifest), "--platform", "claude"])
    skill_md.write_text("# commit v2, changed upstream")

    _run_ok(["apply", str(manifest), "--platform", "claude"])

    installed = tmp_path / ".claude" / "skills" / "commit" / "SKILL.md"
    assert installed.read_text() == "# commit v2, changed upstream"


def test_apply_dry_run_does_not_write_applied_state(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_skill(tmp_path, "skills/commit/SKILL.md", "# commit v1")
    manifest = _write_manifest(
        tmp_path,
        """
        [skills]
        commit = "skills/commit/SKILL.md"
        """,
    )

    _run_ok(["apply", str(manifest), "--platform", "claude", "--dry-run"])

    assert not default_applied_state_path(manifest).exists()


def test_validate_reports_stale_skill_and_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill_md = _write_skill(tmp_path, "skills/commit/SKILL.md", "# commit v1")
    manifest = _write_manifest(
        tmp_path,
        """
        [skills]
        commit = "skills/commit/SKILL.md"
        """,
    )
    _run_ok(["apply", str(manifest), "--platform", "claude"])
    skill_md.write_text("# commit v2, changed upstream")

    with pytest.raises(SystemExit) as exc_info:
        app(["validate", str(manifest), "--platform", "claude"])

    assert exc_info.value.code == 1
    assert "skills:commit" in capsys.readouterr().out


def test_validate_no_staleness_when_source_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_skill(tmp_path, "skills/commit/SKILL.md", "# commit v1")
    manifest = _write_manifest(
        tmp_path,
        """
        [skills]
        commit = "skills/commit/SKILL.md"
        """,
    )
    _run_ok(["apply", str(manifest), "--platform", "claude"])

    with pytest.raises(SystemExit) as exc_info:
        app(["validate", str(manifest), "--platform", "claude"])

    assert exc_info.value.code == 0
