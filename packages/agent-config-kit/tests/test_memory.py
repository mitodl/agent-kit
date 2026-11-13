"""Reading each platform's agent-written memory store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_config_kit import registry
from agent_config_kit.adapters import claude, codex, copilot


def _claude_memory(
    projects: Path,
    project_path: Path,
    stem: str,
    *,
    type: str = "project",
    description: str = "a one-line summary",
    body: str = "the body of the memory",
) -> Path:
    folder = projects / claude.encode_project(str(project_path)) / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.md"
    path.write_text(
        f"---\nname: {stem}\ndescription: {description}\n"
        f"metadata:\n  type: {type}\n  originSessionId: abc\n---\n\n{body}\n"
    )
    return path


# ── the registry ─────────────────────────────────────────────────────────────


def test_each_platform_names_its_memory_store_or_none(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    roots = {
        name: (p.memory.root if p.memory else None)
        for name in registry.known_platforms()
        if (p := registry.get_platform(name))
    }
    assert roots == {
        "claude": tmp_path / ".claude" / "projects",
        "codex": tmp_path / ".codex" / "memories",
        "copilot": tmp_path / ".config" / "Code" / "User" / "workspaceStorage",
        "pi": None,
        "opencode": None,
    }


def test_config_dir_overrides_move_the_stores(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert (
        registry.get_platform("claude").memory.root == tmp_path / "claude" / "projects"
    )
    assert registry.get_platform("codex").memory.root == tmp_path / "codex" / "memories"


def test_an_absent_store_has_no_entries(tmp_path):
    store = registry.get_platform("claude").memory
    assert not store.root.exists()
    assert store.entries() == []


# ── Claude Code ──────────────────────────────────────────────────────────────


def test_claude_entries_carry_the_directories_their_project_names(tmp_path):
    projects = tmp_path / "projects"
    checkout = tmp_path / "src" / "my.repo"
    checkout.mkdir(parents=True)
    path = _claude_memory(projects, checkout, "fact")
    (path.parent / claude.MEMORY_INDEX).write_text("- [fact](fact.md)\n")

    (entry,) = claude.read_memory(projects)
    assert entry.path == path
    assert entry.key == "fact"
    assert entry.project == claude.encode_project(str(checkout))
    assert entry.candidates == (checkout,)
    assert entry.type == "project"
    assert entry.content == "a one-line summary\n\nthe body of the memory"


def test_a_name_two_directories_encode_to_has_both_as_candidates(tmp_path):
    """``x-y`` and ``x.y`` both encode to ``x-y``; picking one is the caller's call."""
    (tmp_path / "x-y").mkdir()
    (tmp_path / "x.y").mkdir()
    assert claude.project_candidates(claude.encode_project(str(tmp_path / "x-y"))) == (
        tmp_path / "x-y",
        tmp_path / "x.y",
    )


def test_a_moved_directory_has_no_candidates(tmp_path):
    assert (
        claude.project_candidates(claude.encode_project(str(tmp_path / "gone"))) == ()
    )


@pytest.mark.parametrize(
    ("project", "start"),
    [
        ("-home-me-repo", (Path("/"), "home-me-repo")),
        ("C--Users-me-repo", (Path("C:\\"), "Users-me-repo")),
        ("relative-name", None),
    ],
)
def test_project_root_reads_posix_and_windows_drive_names(project, start):
    assert claude.project_root(project) == start


def test_claude_reads_nested_and_top_level_type(tmp_path):
    nested = _claude_memory(tmp_path, tmp_path / "p", "nested", type="feedback")
    flat = tmp_path / "flat.md"
    flat.write_text("---\nname: flat\ndescription: d\ntype: reference\n---\nbody\n")

    assert claude.parse_memory(nested, "p").type == "feedback"
    assert claude.parse_memory(flat, "p").type == "reference"


def test_unquoted_frontmatter_that_is_not_yaml_still_parses(tmp_path):
    """Claude Code writes frontmatter by hand; a value opening with a quoted
    phrase is invalid YAML but its meaning is plain."""
    path = tmp_path / "m.md"
    path.write_text(
        '---\nname: ship\ndescription: "Ship PR N" means ready, not merge\n'
        "metadata:\n  type: feedback\n---\nbody\n"
    )
    parsed = claude.parse_memory(path, "p")
    assert parsed.error is None
    assert parsed.description == '"Ship PR N" means ready, not merge'


def test_a_hash_in_an_unquoted_value_is_not_a_comment(tmp_path):
    """As YAML, `` #`` would start a comment and drop the rest of the value."""
    path = tmp_path / "m.md"
    path.write_text(
        "---\nname: Ship PR #614 first\ndescription: 'quoted: kept whole'\n"
        "type: project\n---\nb\n"
    )
    parsed = claude.parse_memory(path, "p")
    assert parsed.name == "Ship PR #614 first"
    assert parsed.description == "quoted: kept whole"


def test_a_block_scalar_value_keeps_its_continuation_lines(tmp_path):
    path = tmp_path / "m.md"
    path.write_text(
        "---\nname: folded\ndescription: >-\n  first line\n  second line\n"
        "metadata:\n  type: project\n---\nb\n"
    )
    parsed = claude.parse_memory(path, "p")
    assert parsed.error is None
    assert parsed.description == "first line second line"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("# Feedback\n\nType: feedback\n", "no frontmatter"),
        ("---\nname: x\nmetadata:\n  type: secret\n---\nb\n", "unknown type 'secret'"),
        ("---\nname: x\n---\nb\n", "no type"),
        ("---\ndescription: d\ntype: project\n---\nb\n", "frontmatter has no name"),
        (
            "---\nname: x\ndescription: >-\n  a\n b: [\n---\nb\n",
            "frontmatter is not valid YAML",
        ),
    ],
)
def test_unreadable_claude_files_carry_a_reason(tmp_path, text, error):
    path = tmp_path / "m.md"
    path.write_text(text)
    assert claude.parse_memory(path, "p").error == error


# ── Codex ────────────────────────────────────────────────────────────────────


def _raw_memory(thread: str, cwd: Path, task: str, stem: str) -> str:
    return (
        f"## Thread `{thread}`\n"
        "updated_at: 2026-11-01T10:00:00+00:00\n"
        f"cwd: {cwd}\n"
        f"rollout_path: /x/.codex/sessions/{thread}.jsonl\n"
        f"rollout_summary_file: {stem}.md\n"
        "\n"
        "---\n"
        f"description: {task}, and what it taught\n"
        f"task: {task}\n"
        "task_group: repo\n"
        "task_outcome: success\n"
        f"cwd: {cwd}\n"
        "keywords: uv, pytest\n"
        "---\n"
        "\n"
        "### Task 1: run the suite\n\n"
        "Reusable knowledge:\n- run suites with `just test-*`\n\n"
    )


def test_codex_reads_one_entry_per_thread_scoped_to_its_cwd(tmp_path):
    checkout = tmp_path / "src" / "repo"
    checkout.mkdir(parents=True)
    root = tmp_path / "memories"
    root.mkdir()
    (root / codex.RAW_MEMORIES_FILE).write_text(
        "# Raw Memories\n\n"
        "Merged stage-1 raw memories (stable ascending thread-id order):\n\n"
        + _raw_memory(
            "t-1", checkout, "fix the flaky test", "2026-11-01T10-00-00-ab12-flaky"
        )
        + _raw_memory(
            "t-2", tmp_path / "gone", "tidy the docs", "2026-11-02T10-00-00-cd34-docs"
        )
    )
    (root / "MEMORY.md").write_text("# Task Group: repo\n")

    first, second = codex.read_memory(root)
    assert first.key == "2026-11-01T10-00-00-ab12-flaky"
    assert first.project == str(checkout)
    assert first.candidates == (checkout,)
    assert first.name == "fix the flaky test"
    assert first.description == "fix the flaky test, and what it taught"
    assert first.type == "feedback"
    assert first.body.startswith("### Task 1: run the suite")
    assert "## Thread" not in first.body
    assert second.candidates == ()


def test_codex_with_no_raw_memories_yet_has_no_entries(tmp_path):
    (tmp_path / codex.RAW_MEMORIES_FILE).write_text(
        "# Raw Memories\n\nNo raw memories yet.\n"
    )
    assert codex.read_memory(tmp_path) == []
    assert codex.read_memory(tmp_path / "absent") == []


def test_a_raw_memory_without_frontmatter_is_an_error(tmp_path):
    (tmp_path / codex.RAW_MEMORIES_FILE).write_text(
        "## Thread `t-1`\ncwd: /x\nrollout_summary_file: s.md\n\njust prose\n"
    )
    (entry,) = codex.read_memory(tmp_path)
    assert entry.key == "s"
    assert entry.error == "no frontmatter"


# ── GitHub Copilot ───────────────────────────────────────────────────────────


def _copilot_storage(root: Path, wid: str, meta: dict | None) -> Path:
    storage = root / wid
    folder = storage / copilot.REPO_MEMORY_DIR
    folder.mkdir(parents=True)
    if meta is not None:
        (storage / "workspace.json").write_text(json.dumps(meta))
    return folder


def test_copilot_reads_repository_memory_scoped_to_its_workspace(tmp_path):
    checkout = tmp_path / "src" / "repo"
    checkout.mkdir(parents=True)
    root = tmp_path / "workspaceStorage"
    folder = _copilot_storage(root, "abc", {"folder": checkout.as_uri()})
    (folder / "build.md").write_text("# Build commands\n\n- `just test`\n")
    (folder / "notes.md").write_text("- prefer uv\n")
    # User and session memory are not repository memory.
    (folder.parent / "session-1").mkdir()
    (folder.parent / "session-1" / "plan.md").write_text("# plan\n")
    (root / "empty").mkdir()

    build, notes = copilot.read_memory(root)
    assert (build.key, build.name, build.type) == ("build", "Build commands", "project")
    assert build.project == str(checkout)
    assert build.candidates == (checkout,)
    assert build.body == "# Build commands\n\n- `just test`"
    assert notes.name == "notes"


@pytest.mark.parametrize(
    ("meta", "project"),
    [
        (
            {"folder": "vscode-remote://wsl%2Bmanjaro/home/me/repo"},
            "vscode-remote://wsl%2Bmanjaro/home/me/repo",
        ),
        (
            {"workspace": "file:///home/me/all.code-workspace"},
            "file:///home/me/all.code-workspace",
        ),
        (None, "abc"),
    ],
)
def test_a_workspace_without_a_local_folder_has_no_candidates(tmp_path, meta, project):
    folder = _copilot_storage(tmp_path, "abc", meta)
    (folder / "m.md").write_text("a fact\n")
    (entry,) = copilot.read_memory(tmp_path)
    assert entry.project == project
    assert entry.candidates == ()


def test_an_empty_copilot_memory_is_an_error(tmp_path):
    folder = _copilot_storage(tmp_path, "abc", None)
    (folder / "m.md").write_text("\n")
    (entry,) = copilot.read_memory(tmp_path)
    assert entry.error == "empty"
