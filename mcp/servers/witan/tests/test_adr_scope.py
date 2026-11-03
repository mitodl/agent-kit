"""`adr-scope.sh` answers "which ADRs govern this, and do their globs work?".

Real git, in a `tmp_path` repo, because every check in the script is a pathspec
and `:(glob)` semantics are the whole point -- a fake would test the fake.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "witan/skills/write-adr/scripts/adr-scope.sh"
)

requires_git = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git and bash are needed",
)


def sh(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=False
    )


def scope(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return sh(repo, "bash", str(SCRIPT), *args)


def adr(repo: Path, name: str, body: str) -> None:
    (repo / "docs/adr" / name).write_text(body)


def commit(repo: Path, message: str) -> None:
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with one ADR of each kind the script has to tell apart."""
    sh(tmp_path, "git", "init", "-q", ".")
    sh(tmp_path, "git", "config", "user.email", "t@example.invalid")
    sh(tmp_path, "git", "config", "user.name", "t")
    for d in ("docs/adr", "src/api", "src/web"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "src/api/views.py").write_text("def v(): pass\n")
    (tmp_path / "src/web/app.js").write_text("const a = 1;\n")

    adr(tmp_path, "0001-api.md", '---\napplies_to:\n  - "src/api/**"\n---\n\n# 1. API\n')
    adr(tmp_path, "0002-web.md", '---\napplies_to:\n  - "src/web/**"\n---\n\n# 2. Web\n')
    adr(tmp_path, "0003-inherited.md", "# 3. Inherited\n\nNo frontmatter at all.\n")
    adr(
        tmp_path,
        "0004-notify.md",
        '---\napplies_to:\n  - "src/notify/**"\n---\n'
        "<!-- applies_to: src/notify/** is created by this work -->\n\n# 4. Notify\n",
    )
    adr(tmp_path, "0005-nope.md", '---\napplies_to:\n  - "src/nope/**"\n---\n\n# 5. Nope\n')
    commit(tmp_path, "base")
    return tmp_path


@requires_git
def test_changed_names_only_the_adr_whose_glob_the_diff_touches(repo):
    (repo / "src/api/views.py").write_text("def v(): return 1\n")
    commit(repo, "touch the api")

    r = scope(repo, "--changed", "HEAD~1")
    assert "governs this change: 0001-api.md" in r.stdout
    assert "0002-web.md" not in r.stdout


@requires_git
def test_the_unscoped_list_is_printed_even_when_nothing_governs(repo):
    """0 of witan's 11 ADRs carry applies_to, so "nothing governs this" is what
    a correct run routinely prints. It is a conclusion only once the unscoped
    records have been read, so the script never lets it stand alone."""
    (repo / "README.md").write_text("unrelated\n")
    commit(repo, "touch nothing scoped")

    r = scope(repo, "--changed", "HEAD~1")
    assert "No ADR with applies_to governs this change." in r.stdout
    assert "UNSCOPED" in r.stdout
    assert "0003-inherited.md" in r.stdout


@requires_git
def test_an_unterminated_or_prose_applies_to_is_not_read_as_metadata(repo):
    """An ADR *about* this convention quotes `applies_to:` in its prose. Only
    the frontmatter is metadata, or such a record scopes itself by accident."""
    adr(
        repo,
        "0006-about.md",
        "# 6. About applies_to\n\nWe write:\n\n```yaml\napplies_to:\n  - "
        '"src/api/**"\n```\n',
    )
    commit(repo, "an ADR about the convention")

    r = scope(repo, "--changed", "HEAD~1")
    assert "0006-about.md" in r.stdout, "it should land in the UNSCOPED list"
    assert "governs this change: 0006-about.md" not in r.stdout


@requires_git
def test_paths_matches_a_chunks_touches_before_its_first_edit(repo):
    """An ADR found after the code is written is a rewrite; found before, it is
    a constraint. Nothing has changed here -- the chunk has not started."""
    r = scope(repo, "--paths", "src/web/**", "--quiet")
    assert "governs this change: 0002-web.md" in r.stdout
    assert "0001-api.md" not in r.stdout


@requires_git
def test_a_touches_glob_matching_nothing_is_reported_not_silently_dropped(repo):
    r = scope(repo, "--paths", "src/unbuilt/**", "--quiet")
    assert "matches no tracked file: src/unbuilt/**" in r.stderr
    assert "judge by hand" in r.stderr


@requires_git
def test_a_glob_matching_nothing_with_no_note_is_a_warning_and_exit_2(repo):
    r = scope(repo, "--changed", "HEAD", "--quiet")
    assert r.returncode == 2
    assert "0005-nope.md" in r.stderr
    # The declared one is reported differently and does not raise the status.
    assert "Taken at its word" in r.stderr


@requires_git
def test_a_forward_note_is_taken_at_its_word_until_sweep(repo):
    """At the START of the work you cannot tell a wrong glob from a path the
    work has not created yet. Only --sweep, after every chunk has merged, can."""
    mid = scope(repo, "--changed", "HEAD", "--quiet")
    assert "Taken at its word" in mid.stderr
    assert mid.returncode == 2, "only 0005 is wrong at this point"

    swept = scope(repo, "--sweep", "--quiet")
    assert swept.returncode == 3
    assert "WRONG GLOB: 0004-notify.md" in swept.stderr


@requires_git
def test_sweep_retires_a_note_once_the_path_actually_lands(repo):
    (repo / "src/notify").mkdir()
    (repo / "src/notify/send.py").write_text("def s(): pass\n")
    commit(repo, "the chunk that creates it")

    r = scope(repo, "--sweep", "--quiet")
    # Findings and actions go to stderr; stdout carries the answer. Both lines
    # of this one must land on the same stream -- they were split once.
    assert "LIVE: 0004-notify.md" in r.stderr
    assert "drop the\n      'created by this work' note." in r.stderr
    assert r.returncode == 2, "0005 is still wrong; 0004 no longer is"


@requires_git
def test_the_glob_star_trap_is_caught_because_the_script_applies_the_prefix(repo):
    """`src/*.py` looks narrow and, without `:(glob)`, matches a whole subtree.
    The script applies the pathspec prefix itself so a caller cannot forget it
    -- which makes a glob written the wrong way show up as governing nothing
    rather than as silently governing everything."""
    adr(repo, "0007-trap.md", '---\napplies_to:\n  - "src/*.py"\n---\n\n# 7. Trap\n')
    commit(repo, "a narrow-looking glob")

    # Without the prefix git would match src/api/views.py here.
    loose = sh(repo, "git", "ls-files", "--", "src/*.py")
    assert "src/api/views.py" in loose.stdout

    r = scope(repo, "--changed", "HEAD~1", "--quiet")
    assert "0007-trap.md" in r.stderr
    assert "governs this change: 0007-trap.md" not in r.stdout


@requires_git
def test_it_refuses_rather_than_guessing_outside_a_work_tree(tmp_path):
    (tmp_path / "docs/adr").mkdir(parents=True)
    r = scope(tmp_path, "--sweep")
    assert r.returncode == 1
    assert "not inside a git work tree" in r.stderr


@requires_git
def test_an_unresolvable_base_ref_is_an_error_not_an_empty_diff(repo):
    r = scope(repo, "--changed", "no-such-ref")
    assert r.returncode == 1
    assert "cannot resolve base ref" in r.stderr
