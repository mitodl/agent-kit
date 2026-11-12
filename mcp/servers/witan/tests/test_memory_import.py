"""``witan memory import``: Claude Code memory files into the shared graph."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from witan import memory_import as local
from witan.cli import _common
from witan.cli import output as output_module
from witan.config import ScanConfig
from witan.scan import write_guard_from_config

from .conftest import requires_omnigraph

REPO = "https://github.com/test/repo"
OTHER_REPO = "https://github.com/test/other"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret gitleaks:allow


def _checkout(path: Path, remote: str | None = REPO) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", remote], check=True
        )
    return path


def _memory(
    projects: Path,
    project_path: Path,
    stem: str,
    *,
    name: str | None = None,
    type: str = "project",
    description: str = "a one-line summary",
    body: str = "the body of the memory",
) -> Path:
    folder = projects / local.encode(str(project_path)) / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.md"
    path.write_text(
        f"---\nname: {name or stem}\ndescription: {description}\n"
        f"metadata:\n  type: {type}\n  originSessionId: abc\n---\n\n{body}\n"
    )
    return path


# ── project directory → repo ─────────────────────────────────────────────────


def test_a_checkout_resolves_to_its_origin(tmp_path):
    checkout = _checkout(tmp_path / "code" / "agent-kit.worktrees" / "wt_1")
    resolved = local.resolve_project(local.encode(str(checkout)))
    assert resolved.repo == REPO
    assert resolved.checkout == checkout


def test_a_name_two_checkouts_encode_to_is_ambiguous(tmp_path):
    """``x-y`` and ``x.y`` both encode to ``x-y``; picking one would be a guess."""
    _checkout(tmp_path / "x-y", REPO)
    _checkout(tmp_path / "x.y", OTHER_REPO)
    resolved = local.resolve_project(local.encode(str(tmp_path / "x-y")))
    assert resolved.repo is None
    assert resolved.reason.startswith("ambiguous")


def test_two_matches_on_one_repo_are_not_ambiguous(tmp_path):
    _checkout(tmp_path / "x-y", REPO)
    _checkout(tmp_path / "x.y", REPO)
    assert local.resolve_project(local.encode(str(tmp_path / "x-y"))).repo == REPO


def test_a_directory_inside_a_checkout_does_not_borrow_its_remote(tmp_path):
    """A folder of checkouts under a dotfile-managed home is not that repo."""
    _checkout(tmp_path / "home")
    inner = tmp_path / "home" / "code"
    inner.mkdir()
    resolved = local.resolve_project(local.encode(str(inner)))
    assert resolved.repo is None
    assert "not a git checkout" in resolved.reason


def test_a_checkout_without_a_remote_is_unresolved(tmp_path):
    checkout = _checkout(tmp_path / "bare", remote=None)
    assert local.resolve_project(local.encode(str(checkout))).repo is None


def test_a_moved_directory_is_unresolved(tmp_path):
    resolved = local.resolve_project(local.encode(str(tmp_path / "gone")))
    assert resolved.repo is None
    assert resolved.reason == "no directory on disk matches this name"


# ── parsing ──────────────────────────────────────────────────────────────────


def test_parse_reads_nested_and_top_level_type(tmp_path):
    nested = _memory(tmp_path, tmp_path / "p", "nested", type="feedback")
    flat = tmp_path / "flat.md"
    flat.write_text("---\nname: flat\ndescription: d\ntype: reference\n---\nbody\n")

    assert local.parse(nested, "p").kind == "lesson"
    assert local.parse(flat, "p").kind == "project_fact"
    assert (
        local.parse(nested, "p").content
        == "a one-line summary\n\nthe body of the memory"
    )


def test_unquoted_frontmatter_that_is_not_yaml_still_parses(tmp_path):
    """Claude Code writes frontmatter by hand; a value opening with a quoted
    phrase is invalid YAML but its meaning is plain."""
    path = tmp_path / "m.md"
    path.write_text(
        '---\nname: ship\ndescription: "Ship PR N" means ready, not merge\n'
        "metadata:\n  type: feedback\n---\nbody\n"
    )
    parsed = local.parse(path, "p")
    assert parsed.error is None
    assert parsed.description == '"Ship PR N" means ready, not merge'
    assert parsed.kind == "lesson"


def test_a_hash_in_an_unquoted_value_is_not_a_comment(tmp_path):
    """As YAML, `` #`` would start a comment and drop the rest of the value."""
    path = tmp_path / "m.md"
    path.write_text(
        "---\nname: Ship PR #614 first\ndescription: 'quoted: kept whole'\n"
        "type: project\n---\nb\n"
    )
    parsed = local.parse(path, "p")
    assert parsed.name == "Ship PR #614 first"
    assert parsed.description == "quoted: kept whole"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("# Feedback\n\nType: feedback\n", "no frontmatter"),
        ("---\nname: x\nmetadata:\n  type: secret\n---\nb\n", "unknown type 'secret'"),
        ("---\nname: x\n---\nb\n", "no type"),
        ("---\ndescription: d\ntype: project\n---\nb\n", "frontmatter has no name"),
    ],
)
def test_unimportable_files_carry_a_reason(tmp_path, text, error):
    path = tmp_path / "m.md"
    path.write_text(text)
    assert local.parse(path, "p").error == error


def test_discover_skips_the_index(tmp_path):
    path = _memory(tmp_path, tmp_path / "p", "one")
    (path.parent / local.INDEX_FILE).write_text("- [one](one.md)\n")
    assert [m.path for m in local.discover(tmp_path)] == [path]


# ── the command, against a real store ────────────────────────────────────────


@pytest.fixture
def cli(server, monkeypatch, capsys):
    """The command under ``--output-format json``, returning its rows by id.

    The guard is what ``server`` leaves off; the import's promise that every
    write is scanned is only tested with one on.
    """
    from witan import server as srv
    from witan.cli.memory import memory_import

    monkeypatch.setattr(_common, "_server", server)
    monkeypatch.setattr(srv.client, "guard", write_guard_from_config(ScanConfig()))
    output_module.set_output_format("json")

    def run(**kwargs) -> dict[str, dict]:
        capsys.readouterr()
        try:
            memory_import(**kwargs)
        finally:
            out = capsys.readouterr().out
        return {row["id"]: row for row in json.loads(out)["rows"]}

    yield run
    output_module.set_output_format("txt")


@pytest.fixture
def projects(tmp_path):
    """One resolvable project with a fact, a feedback and a user memory, and one
    project whose directory no longer exists."""
    root = tmp_path / "projects"
    checkout = _checkout(tmp_path / "src" / "repo")
    _memory(root, checkout, "fact", name="Fact: the cache lives on disk")
    _memory(root, checkout, "feedback", type="feedback", body="do not do the thing")
    _memory(root, checkout, "who", type="user", body="prefers terse replies")
    _memory(root, tmp_path / "moved", "lost")
    return root


def _titles(server) -> list[str]:
    return sorted(m["title"] for m in server.memory_list(repo=""))


@requires_omnigraph
def test_dry_run_lists_candidates_and_writes_nothing(cli, projects, server):
    rows = cli(source=projects)

    assert {k: (r["kind"], r["repo"], r["status"]) for k, r in rows.items()} == {
        "test/repo/fact": ("project_fact", REPO, "new"),
        "test/repo/feedback": ("lesson", REPO, "new"),
        "test/repo/who": ("agent_context", REPO, "user"),
        f"{local.encode(str(projects.parent / 'moved')).lstrip('-')}/lost": (
            "project_fact",
            None,
            "unresolved",
        ),
    }
    assert _titles(server) == []


@requires_omnigraph
def test_confirmed_import_stores_the_selection_and_a_rerun_stores_nothing(
    cli, projects, server
):
    first = cli(source=projects, all_=True, confirm=True)
    assert first["test/repo/fact"]["status"] == "stored"
    assert first["test/repo/feedback"]["status"] == "stored"
    assert first["test/repo/who"]["status"] == "user", "--all must skip user memories"
    assert _titles(server) == ["Fact: the cache lives on disk", "feedback"]

    stored = server.memory_get(first["test/repo/feedback"]["slug"])
    assert stored["kind"] == "lesson"
    assert stored["repo"] == REPO
    from witan import server as srv

    assert stored["author"] == srv._current_author()

    second = cli(source=projects, all_=True, confirm=True)
    assert second["test/repo/fact"]["status"] == "duplicate"
    assert second["test/repo/fact"]["slug"] == first["test/repo/fact"]["slug"]
    assert second["test/repo/feedback"]["status"] == "duplicate"
    assert len(_titles(server)) == 2


@requires_omnigraph
def test_a_user_memory_is_stored_only_when_named_exactly(cli, projects, server):
    globbed = cli(source=projects, select=["test/repo/*"], confirm=True)
    assert globbed["test/repo/who"]["status"] == "user"

    named = cli(source=projects, select=["who"], confirm=True)
    assert named["test/repo/who"]["status"] == "stored"
    assert server.memory_get(named["test/repo/who"]["slug"])["kind"] == "agent_context"


@requires_omnigraph
def test_select_without_confirm_is_still_a_dry_run(cli, projects, server):
    rows = cli(source=projects, select=["fact"])
    assert rows["test/repo/fact"]["status"] == "would store"
    assert rows["test/repo/feedback"]["status"] == "new"
    assert _titles(server) == []


@requires_omnigraph
def test_the_scanner_blocks_a_secret_and_the_run_says_so(
    cli, tmp_path, projects, server
):
    checkout = tmp_path / "src" / "repo"
    _memory(projects, checkout, "leak", body=f"the deploy key is {AWS_KEY}")

    with pytest.raises(SystemExit) as exc:
        cli(source=projects, select=["leak"], confirm=True)
    assert exc.value.code == 1
    assert _titles(server) == []


@requires_omnigraph
def test_the_scanner_result_is_reported_per_row(cli, tmp_path, projects, capsys):
    checkout = tmp_path / "src" / "repo"
    _memory(projects, checkout, "leak", body=f"the deploy key is {AWS_KEY}")
    from witan.cli.memory import memory_import

    with pytest.raises(SystemExit):
        memory_import(source=projects, select=["leak", "fact"], confirm=True)
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)["rows"]}
    assert rows["test/repo/leak"]["status"] == "blocked"
    assert "aws_access_key" in rows["test/repo/leak"]["detail"]
    assert AWS_KEY not in rows["test/repo/leak"]["detail"]
    assert rows["test/repo/fact"]["status"] == "stored"


@requires_omnigraph
def test_a_redacted_memory_is_still_a_duplicate_on_rerun(
    cli, tmp_path, projects, server
):
    """The scanner rewrites the stored title and content, so they no longer
    equal the file; without matching through the redaction, every rerun
    stores another copy."""
    checkout = tmp_path / "src" / "repo"
    _memory(
        projects,
        checkout,
        "mail",
        name="Alerts go to ops@example.org",
        description="Alerts go to ops@example.org",
        body="Page ops@example.org first.",
    )
    first = cli(source=projects, select=["mail"], confirm=True)
    assert first["test/repo/mail"]["status"] == "stored"
    assert "redacted" in server.memory_get(first["test/repo/mail"]["slug"])["title"]
    assert first["test/repo/mail"]["detail"].endswith("(redacted: content, title)")
    assert "ALTERED" in first["test/repo/mail"]["redaction_note"]

    second = cli(source=projects, select=["mail"], confirm=True)
    assert second["test/repo/mail"]["status"] == "duplicate"
    assert len(_titles(server)) == 1


@requires_omnigraph
def test_an_exact_title_is_found_under_many_stronger_content_hits(
    cli, projects, server
):
    """recall's title-only hits rank below its content hits, so a top-20
    cut lost the one memory whose title is an exact match."""
    for i in range(25):
        server.memory_store(
            kind="project_fact",
            title=f"note {i}",
            content="the cache lives on disk, the cache lives on disk",
            repo=REPO,
        )
    existing = server.memory_store(
        kind="project_fact",
        title="Fact: the cache lives on disk",
        content="worded differently",
        repo=REPO,
    )
    rows = cli(source=projects)
    assert rows["test/repo/fact"]["status"] == "duplicate"
    assert rows["test/repo/fact"]["slug"] == existing["slug"]


@requires_omnigraph
def test_one_stem_in_two_checkouts_of_a_repo_gets_two_ids(
    cli, tmp_path, projects, server
):
    worktree = _checkout(tmp_path / "src" / "repo.worktrees" / "wt")
    _memory(projects, tmp_path / "src" / "repo", "note", body="from the checkout")
    _memory(projects, worktree, "note", name="other note", body="from the worktree")

    rows = cli(source=projects)
    notes = sorted(k for k in rows if k.endswith("/note"))
    assert len(notes) == 2
    assert not any(k.startswith("test/repo/") for k in notes)

    stored = cli(source=projects, select=[notes[0]], confirm=True)
    assert [stored[k]["status"] for k in notes] == ["stored", "new"]


@requires_omnigraph
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"confirm": True}, "--confirm writes only what --select or --all chooses."),
        ({"select": ["nope"]}, "--select matched no memory: nope"),
    ],
)
def test_refusals(cli, projects, monkeypatch, kwargs, message):
    errors = []
    monkeypatch.setattr(
        "witan.cli.memory.print_error", lambda msg, **_: errors.append(str(msg))
    )
    with pytest.raises(SystemExit):
        cli(source=projects, **kwargs)
    assert errors == [message]
