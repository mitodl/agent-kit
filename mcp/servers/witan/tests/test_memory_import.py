"""``witan memory import``: agents' on-disk memory into the shared graph.

Reading each agent's store is agent-config-kit's, and tested there
(packages/agent-config-kit/tests/test_memory.py). These cover the witan side:
resolving a project to a repo, and the command against a real graph.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from agent_config_kit import MemoryEntry
from agent_config_kit.adapters import claude, codex, copilot
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
    """A Claude Code auto-memory file."""
    folder = projects / claude.encode_project(str(project_path)) / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.md"
    path.write_text(
        f"---\nname: {name or stem}\ndescription: {description}\n"
        f"metadata:\n  type: {type}\n  originSessionId: abc\n---\n\n{body}\n"
    )
    return path


def _resolve(path: Path) -> local.Resolution:
    """Resolve the Claude Code project directory ``path`` would be stored under."""
    project = claude.encode_project(str(path))
    return local.resolve(
        MemoryEntry(path, "m", project, claude.project_candidates(project))
    )


# ── project directory → repo ─────────────────────────────────────────────────


def test_a_checkout_resolves_to_its_origin(tmp_path):
    checkout = _checkout(tmp_path / "code" / "agent-kit.worktrees" / "wt_1")
    resolved = _resolve(checkout)
    assert resolved.repo == REPO
    assert resolved.checkout == checkout


def test_a_name_two_checkouts_encode_to_is_ambiguous(tmp_path):
    """``x-y`` and ``x.y`` both encode to ``x-y``; picking one would be a guess."""
    _checkout(tmp_path / "x-y", REPO)
    _checkout(tmp_path / "x.y", OTHER_REPO)
    resolved = _resolve(tmp_path / "x-y")
    assert resolved.repo is None
    assert resolved.reason.startswith("ambiguous")


def test_two_matches_on_one_repo_are_not_ambiguous(tmp_path):
    _checkout(tmp_path / "x-y", REPO)
    _checkout(tmp_path / "x.y", REPO)
    assert _resolve(tmp_path / "x-y").repo == REPO


def test_a_directory_inside_a_checkout_does_not_borrow_its_remote(tmp_path):
    """A folder of checkouts under a dotfile-managed home is not that repo."""
    _checkout(tmp_path / "home")
    inner = tmp_path / "home" / "code"
    inner.mkdir()
    resolved = _resolve(inner)
    assert resolved.repo is None
    assert "not a git checkout" in resolved.reason


def test_a_checkout_without_a_remote_is_unresolved(tmp_path):
    assert _resolve(_checkout(tmp_path / "bare", remote=None)).repo is None


def test_a_moved_directory_is_unresolved(tmp_path):
    resolved = _resolve(tmp_path / "gone")
    assert resolved.repo is None
    assert resolved.reason == "no directory on disk matches this project"


# ── --map: a project's repo given on the command line ───────────────────────


def test_a_map_canonicalises_its_repo_like_any_explicit_repo():
    assert local.parse_maps(
        ["-home-me-gone=git@github.com:Test/Repo.git", "/srv/old=" + OTHER_REPO + "/"]
    ) == {"-home-me-gone": REPO, "/srv/old": OTHER_REPO}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("no-equals-sign", "is not <project-dir>=<repo-uri>"),
        (f"={REPO}", "is not <project-dir>=<repo-uri>"),
        ("-home-me-gone=", "'' is not a repo URI"),
        ("-home-me-gone=test/repo", "'test/repo' is not a repo URI"),
        ("-home-me-gone=https://github.com", "is not a repo URI"),
    ],
)
def test_a_map_without_a_repo_uri_is_refused(value, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        local.parse_maps([value])


def test_one_project_mapped_to_two_repos_is_refused():
    with pytest.raises(ValueError, match="two repos"):
        local.parse_maps([f"-x={REPO}", f"-x={OTHER_REPO}"])


def test_a_map_names_a_project_by_its_label_or_the_path_it_stood_for(tmp_path):
    gone = tmp_path / "code" / "app.worktrees" / "wt_1"
    label = claude.encode_project(str(gone))
    maps = {label: REPO, f"{gone}/": OTHER_REPO, "relative/dir": REPO}
    assert local.mapped(label, maps) == {label: REPO, f"{gone}/": OTHER_REPO}
    # Codex records the cwd itself.
    assert local.mapped(str(gone), maps) == {f"{gone}/": OTHER_REPO}
    assert local.mapped(claude.encode_project(str(tmp_path)), maps) == {}


@pytest.mark.parametrize(
    ("type", "kind"),
    [
        ("project", "project_fact"),
        ("reference", "project_fact"),
        ("feedback", "lesson"),
        ("user", "agent_context"),
        (None, None),
    ],
)
def test_each_memory_type_maps_to_a_kind(type, kind):
    assert local.kind(MemoryEntry(Path("m.md"), "m", "p", type=type)) == kind


def test_an_agent_without_a_memory_store_says_so():
    assert local.harness("pi").missing == "keeps no memory on disk"


def test_an_absent_or_empty_store_says_which(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert local.harness("codex").missing.startswith("no memory store at ")
    (tmp_path / "codex" / "memories").mkdir(parents=True)
    assert local.harness("codex").missing.startswith("no memories in ")


@pytest.mark.parametrize(
    ("stored", "local_text", "same"),
    [
        ("Alerts go to «redacted:email» now", "Alerts go to ops@example.org now", True),
        ("Alerts go to «redacted:email» now", "Alerts go to ops@example.org", False),
        ("«redacted:email»", "anything at all", False),
        ("plain title", "Plain  Title", True),
    ],
)
def test_a_redacted_span_stands_for_some_text_but_not_all_of_it(
    stored, local_text, same
):
    from witan.cli.memory import _same

    assert _same(stored, local_text) is same


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
def projects(tmp_path, monkeypatch):
    """Claude Code's store: one resolvable project with a fact, a feedback and a
    user memory, and one project whose directory no longer exists."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    root = tmp_path / "claude" / "projects"
    checkout = _checkout(tmp_path / "src" / "repo")
    _memory(root, checkout, "fact", name="Fact: the cache lives on disk")
    _memory(root, checkout, "feedback", type="feedback", body="do not do the thing")
    _memory(root, checkout, "who", type="user", body="prefers terse replies")
    _memory(root, tmp_path / "moved", "lost")
    return root


def _titles(server) -> list[str]:
    return sorted(m["title"] for m in server.memory_list(all_repos=True))


@requires_omnigraph
def test_dry_run_lists_candidates_and_writes_nothing(cli, projects, server):
    rows = cli()

    assert {k: (r["kind"], r["repo"], r["status"]) for k, r in rows.items()} == {
        "test/repo/fact": ("project_fact", REPO, "new"),
        "test/repo/feedback": ("lesson", REPO, "new"),
        "test/repo/who": ("agent_context", REPO, "user"),
        f"{claude.encode_project(str(projects.parent.parent / 'moved')).strip('-')}/lost": (
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
    first = cli(all_=True, confirm=True)
    assert first["test/repo/fact"]["status"] == "stored"
    assert first["test/repo/feedback"]["status"] == "stored"
    assert first["test/repo/who"]["status"] == "user", "--all must skip user memories"
    assert _titles(server) == ["Fact: the cache lives on disk", "feedback"]

    stored = server.memory_get(first["test/repo/feedback"]["slug"])
    assert stored["kind"] == "lesson"
    assert stored["repo"] == REPO
    from witan import server as srv

    assert stored["author"] == srv._current_author()

    second = cli(all_=True, confirm=True)
    assert second["test/repo/fact"]["status"] == "duplicate"
    assert second["test/repo/fact"]["slug"] == first["test/repo/fact"]["slug"]
    assert second["test/repo/feedback"]["status"] == "duplicate"
    assert len(_titles(server)) == 2


@requires_omnigraph
def test_a_user_memory_is_stored_only_when_named_exactly(cli, projects, server):
    globbed = cli(select=["test/repo/*"], confirm=True)
    assert globbed["test/repo/who"]["status"] == "user"

    named = cli(select=["who"], confirm=True)
    assert named["test/repo/who"]["status"] == "stored"
    assert server.memory_get(named["test/repo/who"]["slug"])["kind"] == "agent_context"


@requires_omnigraph
def test_select_without_confirm_is_still_a_dry_run(cli, projects, server):
    rows = cli(select=["fact"])
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
        cli(select=["leak"], confirm=True)
    assert exc.value.code == 1
    assert _titles(server) == []


@requires_omnigraph
def test_the_scanner_result_is_reported_per_row(cli, tmp_path, projects, capsys):
    checkout = tmp_path / "src" / "repo"
    _memory(projects, checkout, "leak", body=f"the deploy key is {AWS_KEY}")
    from witan.cli.memory import memory_import

    with pytest.raises(SystemExit):
        memory_import(select=["leak", "fact"], confirm=True)
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
    first = cli(select=["mail"], confirm=True)
    assert first["test/repo/mail"]["status"] == "stored"
    assert "redacted" in server.memory_get(first["test/repo/mail"]["slug"])["title"]
    assert first["test/repo/mail"]["detail"].endswith("(redacted: content, title)")
    assert "ALTERED" in first["test/repo/mail"]["redaction_note"]

    second = cli(select=["mail"], confirm=True)
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
    rows = cli()
    assert rows["test/repo/fact"]["status"] == "duplicate"
    assert rows["test/repo/fact"]["slug"] == existing["slug"]


@requires_omnigraph
def test_one_stem_in_two_checkouts_of_a_repo_gets_two_ids(
    cli, tmp_path, projects, server
):
    worktree = _checkout(tmp_path / "src" / "repo.worktrees" / "wt")
    _memory(projects, tmp_path / "src" / "repo", "note", body="from the checkout")
    _memory(projects, worktree, "note", name="other note", body="from the worktree")

    rows = cli()
    notes = sorted(k for k in rows if k.endswith("/note"))
    assert len(notes) == 2
    assert not any(k.startswith("test/repo/") for k in notes)

    stored = cli(select=[notes[0]], confirm=True)
    assert [stored[k]["status"] for k in notes] == ["stored", "new"]


@requires_omnigraph
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"confirm": True}, "--confirm writes only what --select or --all chooses."),
        ({"select": ["nope"]}, "--select matched no memory: nope"),
        ({"harness": ["nope"]}, "--harness nope is not an agent agent-kit knows"),
    ],
)
def test_refusals_go_to_stderr_under_structured_output(
    cli, projects, capsys, kwargs, message
):
    """stdout holds the one JSON document a consumer parses; prose there
    breaks it."""
    from witan.cli.memory import memory_import

    capsys.readouterr()
    with pytest.raises(SystemExit):
        memory_import(**kwargs)
    out, err = capsys.readouterr()
    assert out == ""
    assert message in err


@requires_omnigraph
def test_a_mapped_project_lists_its_memories_under_that_repo(
    cli, tmp_path, projects, capsys
):
    gone = tmp_path / "gone"
    _memory(projects, gone, "orphan")
    moved = claude.encode_project(str(tmp_path / "moved"))

    from witan.cli.memory import memory_import

    capsys.readouterr()
    memory_import(map_=[f"{moved}=git@github.com:test/other.git"])
    out, err = capsys.readouterr()
    rows = {row["id"]: row for row in json.loads(out)["rows"]}
    # Long paths wrap at the pinned terminal width.
    err = "".join(err.split())

    assert (rows["test/other/lost"]["repo"], rows["test/other/lost"]["status"]) == (
        OTHER_REPO,
        "new",
    )
    orphan = rows[f"{claude.encode_project(str(gone)).strip('-')}/orphan"]
    assert (orphan["repo"], orphan["status"]) == (None, "unresolved")
    assert f"unresolvedclaude{claude.encode_project(str(gone))}:" in err
    assert moved not in err
    assert "--map=<project-dir>=<repo-uri>" in err


@requires_omnigraph
def test_a_map_by_absolute_path_overrides_a_resolved_checkout(
    cli, tmp_path, projects, server
):
    rows = cli(map_=[f"{tmp_path / 'src' / 'repo'}={OTHER_REPO}"])
    assert rows["test/other/fact"]["repo"] == OTHER_REPO
    assert rows["test/other/fact"]["status"] == "new"
    assert _titles(server) == []


@requires_omnigraph
def test_a_mapped_project_is_stored_under_that_repo(cli, tmp_path, projects, server):
    moved = tmp_path / "moved"
    rows = cli(map_=[f"{moved}={OTHER_REPO}"], select=["lost"], confirm=True)
    assert rows["test/other/lost"]["status"] == "stored"
    assert server.memory_get(rows["test/other/lost"]["slug"])["repo"] == OTHER_REPO


@requires_omnigraph
@pytest.mark.parametrize(
    ("maps", "message"),
    [
        (["-x=test/repo"], "'test/repo' is not a repo URI"),
        ([f"-nowhere={REPO}"], "--map matched no project: -nowhere"),
    ],
)
def test_a_bad_map_is_refused_before_anything_is_listed(
    cli, projects, capsys, maps, message
):
    from witan.cli.memory import memory_import

    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        memory_import(map_=maps)
    out, err = capsys.readouterr()
    assert exc.value.code == 1
    assert out == ""
    assert message in err


@requires_omnigraph
def test_a_stem_with_glob_characters_is_selectable_by_name(
    cli, tmp_path, projects, server
):
    _memory(projects, tmp_path / "src" / "repo", "contact[team]", type="user")
    rows = cli(select=["contact[team]"], confirm=True)
    assert rows["test/repo/contact[team]"]["status"] == "stored"


@requires_omnigraph
def test_a_wholly_redacted_title_is_still_a_duplicate_on_rerun(
    cli, tmp_path, projects, server
):
    """Nothing of the title survives in the graph, so the lookup has to find
    the stored copy through its description."""
    _memory(
        projects,
        tmp_path / "src" / "repo",
        "addr",
        name="ops@example.org",
        description="Where production alerts are routed",
        body="Page the on-call engineer.",
    )
    first = cli(select=["addr"], confirm=True)
    assert first["test/repo/addr"]["status"] == "stored"

    second = cli(select=["addr"], confirm=True)
    assert second["test/repo/addr"]["status"] == "duplicate"
    assert second["test/repo/addr"]["slug"] == first["test/repo/addr"]["slug"]


# ── other agents' stores ─────────────────────────────────────────────────────


@pytest.fixture
def codex_store(tmp_path, monkeypatch):
    """Codex's memories: one session in the checkout, one in a moved directory."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    root = tmp_path / "codex" / "memories"
    root.mkdir(parents=True)

    def thread(thread_id: str, cwd: Path, task: str, stem: str) -> str:
        return (
            f"## Thread `{thread_id}`\nupdated_at: 2026-11-01T10:00:00+00:00\n"
            f"cwd: {cwd}\nrollout_path: /x/{thread_id}.jsonl\n"
            f"rollout_summary_file: {stem}.md\n\n"
            f"---\ndescription: {task}, and the fix\ntask: {task}\n"
            f"task_group: repo\ntask_outcome: success\ncwd: {cwd}\n"
            "keywords: pytest\n---\n\n### Task 1: x\n\nReusable knowledge:\n- y\n\n"
        )

    (root / codex.RAW_MEMORIES_FILE).write_text(
        "# Raw Memories\n\n"
        + thread("t-1", tmp_path / "src" / "repo", "fix the flaky test", "s-flaky")
        + thread("t-2", tmp_path / "moved", "tidy the docs", "s-docs")
    )
    return root


@pytest.fixture
def copilot_store(tmp_path, monkeypatch):
    """Copilot's repository memory for a workspace on the checkout."""
    from agent_config_kit import registry

    user_dir = tmp_path / "Code" / "User"
    monkeypatch.setattr(registry, "vscode_user_dir", lambda: user_dir)
    storage = user_dir / "workspaceStorage" / "abc"
    folder = storage / copilot.REPO_MEMORY_DIR
    folder.mkdir(parents=True)
    (storage / "workspace.json").write_text(
        json.dumps({"folder": (tmp_path / "src" / "repo").as_uri()})
    )
    (folder / "build.md").write_text("# Build commands\n\n- run `just test`\n")
    return folder


@requires_omnigraph
def test_every_detected_agent_is_read_by_default(
    cli, tmp_path, projects, codex_store, copilot_store, capsys
):
    rows = cli()
    by_harness = {
        k: (r["harness"], r["kind"], r["status"])
        for k, r in rows.items()
        if r["harness"] != "claude"
    }
    moved = str(tmp_path / "moved").strip("/")
    assert by_harness == {
        "test/repo/s-flaky": ("codex", "lesson", "new"),
        f"{moved}/s-docs": ("codex", "lesson", "unresolved"),
        "test/repo/build": ("copilot", "project_fact", "new"),
    }
    assert rows["test/repo/fact"]["status"] == "new"


@requires_omnigraph
def test_other_agents_memories_are_stored_and_a_rerun_stores_nothing(
    cli, projects, codex_store, copilot_store, server
):
    first = cli(harness=["codex", "copilot"], all_=True, confirm=True)
    assert first["test/repo/s-flaky"]["status"] == "stored"
    assert first["test/repo/build"]["status"] == "stored"
    assert "test/repo/fact" not in first, "--harness limits the agents read"

    lesson = server.memory_get(first["test/repo/s-flaky"]["slug"])
    assert (lesson["kind"], lesson["title"]) == ("lesson", "fix the flaky test")
    fact = server.memory_get(first["test/repo/build"]["slug"])
    assert (fact["kind"], fact["title"]) == ("project_fact", "Build commands")

    second = cli(harness=["codex,copilot"], all_=True, confirm=True)
    assert second["test/repo/s-flaky"]["status"] == "duplicate"
    assert second["test/repo/build"]["status"] == "duplicate"


@requires_omnigraph
def test_a_map_applies_to_another_agents_project(cli, tmp_path, projects, codex_store):
    rows = cli(harness=["codex"], map_=[f"{tmp_path / 'moved'}={OTHER_REPO}"])
    assert rows["test/other/s-docs"]["repo"] == OTHER_REPO
    assert rows["test/repo/s-flaky"]["repo"] == REPO


@requires_omnigraph
def test_agents_without_memory_are_named_not_skipped(cli, projects, capsys):
    from witan.cli.memory import memory_import

    capsys.readouterr()
    memory_import(harness=["pi", "opencode", "codex", "claude"])
    out, err = capsys.readouterr()
    rows = json.loads(out)["rows"]
    assert {r["harness"] for r in rows} == {"claude"}
    assert "Pi: keeps no memory on disk." in err
    assert "OpenCode: keeps no memory on disk." in err
    assert "Codex CLI: no memory store at " in err


@requires_omnigraph
def test_one_title_in_two_agents_gets_two_ids_and_one_store(
    cli, tmp_path, projects, copilot_store
):
    """Claude Code and Copilot can both hold a memory named ``fact`` for one
    repo; each needs its own id, and the second is a duplicate of the first."""
    (copilot_store / "fact.md").write_text("# Fact: the cache lives on disk\n\nx\n")
    rows = cli(select=["fact"], confirm=True)
    facts = {k: r for k, r in rows.items() if k.endswith("/fact")}
    assert len(facts) == 2
    assert not any(k.startswith("test/repo/") for k in facts)
    assert sorted(r["status"] for r in facts.values()) == ["duplicate", "stored"]
