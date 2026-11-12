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
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


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

    adr(
        tmp_path, "0001-api.md", '---\napplies_to:\n  - "src/api/**"\n---\n\n# 1. API\n'
    )
    adr(
        tmp_path, "0002-web.md", '---\napplies_to:\n  - "src/web/**"\n---\n\n# 2. Web\n'
    )
    adr(tmp_path, "0003-inherited.md", "# 3. Inherited\n\nNo frontmatter at all.\n")
    adr(
        tmp_path,
        "0004-notify.md",
        '---\napplies_to:\n  - "src/notify/**"\n---\n'
        "<!-- applies_to: src/notify/** is created by this work -->\n\n# 4. Notify\n",
    )
    adr(
        tmp_path,
        "0005-nope.md",
        '---\napplies_to:\n  - "src/nope/**"\n---\n\n# 5. Nope\n',
    )
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
def test_a_touches_glob_is_not_expanded_by_the_shell_before_the_prefix(repo):
    """`--paths 'src/*'` is a pathspec, and `:(glob)src/*` does not cross `/`.

    Unquoted in the loop that splits the argument, the shell pathname-expands
    it FIRST -- `src/*` becomes `src/api src/web`, and either one as a pathspec
    drags in its whole subtree. The widening is silent and lands on the side
    where an ADR is reported as governing a chunk it does not."""
    r = scope(repo, "--paths", "src/*", "--quiet")
    assert "governs this change: " not in r.stdout, r.stdout
    assert "No ADR with applies_to governs this change." in r.stdout
    # Not a vacuous pass: the same globs one level deeper do match.
    deep = scope(repo, "--paths", "src/web/**", "--quiet")
    assert "governs this change: 0002-web.md" in deep.stdout


@requires_git
def test_changed_sees_a_file_the_change_deletes(repo):
    """A diff names paths the change REMOVES, and those are not in the index
    any more. Intersecting `git ls-files` with the diff therefore reported "no
    ADR governs this" on a commit whose whole content was deleting files an
    ADR governs -- the one shape where the ADR most needs reading."""
    sh(repo, "git", "rm", "-q", "src/web/app.js")
    commit(repo, "delete a governed file")

    r = scope(repo, "--changed", "HEAD~1", "--quiet")
    assert "governs this change: 0002-web.md" in r.stdout
    # Its only file is gone, so it also warrants the obsolescence note rather
    # than the "wrong glob" warning -- the glob was right, the files left.
    assert "deletes every file it governed" in r.stderr
    assert (
        "0002-web.md 'src/web/**' matches no tracked file and nothing" not in r.stderr
    )


@requires_git
def test_sweep_rules_only_on_the_records_this_work_wrote(repo):
    """Two projects write forward-looking ADRs; one of them finishes.

    "The work is over" is true of the finishing project and of nothing else in
    the directory, so an unscoped sweep hands back a WRONG GLOB on a record
    whose chunk has not merged -- and the next step is an agent repairing
    somebody else's open decision.
    """
    adr(
        repo,
        "0011-theirs.md",
        '---\napplies_to:\n  - "src/theirs/**"\n---\n'
        "<!-- applies_to: src/theirs/** is created by this work -->\n\n# 11. Theirs\n",
    )
    commit(repo, "another project's in-flight record")

    unscoped = scope(repo, "--sweep", "--quiet")
    assert "WRONG GLOB: 0011-theirs.md" in unscoped.stderr, "the bug, unguarded"

    r = scope(repo, "--sweep", "--quiet", "--records", "0004 0005")
    assert "WRONG GLOB: 0004-notify.md" in r.stderr
    assert r.returncode == 3, "this work's own note is still a verdict"
    assert "WRONG GLOB: 0011-theirs.md" not in r.stderr
    assert "Its sweep, not ours." in r.stderr


@requires_git
@pytest.mark.parametrize(
    "named",
    ["0005", "0005-nope.md", "ADR-0005", "docs/adr/0005-nope.md"],
)
def test_records_names_a_file_by_number_or_filename(repo, named):
    """`Supersedes ADR-0014`, a `Touches:` entry and the index table all carry
    the number; requiring the filename would mean looking it up to pass it."""
    r = scope(repo, "--changed", "HEAD", "--quiet", "--records", named)
    assert r.returncode == 2
    assert "WARN: 0005-nope.md" in r.stderr
    # 0004's forward note is outside the set, so it is reported, not ruled on.
    assert "WRONG GLOB: 0004-notify.md" not in r.stderr


@requires_git
def test_a_broken_glob_outside_the_record_set_is_reported_without_failing(repo):
    r = scope(repo, "--changed", "HEAD", "--quiet", "--records", "0001")
    assert r.returncode == 0, r.stderr
    assert "0005-nope.md" in r.stderr
    assert "reported, not repaired" in r.stderr
    assert "WARN: 0005-nope.md" not in r.stderr


@requires_git
@pytest.mark.parametrize(
    ("status_line", "expected"),
    [
        ("**Status:** Superseded by ADR-0012", "read ADR-0012 for the constraint"),
        ("- Status: Superseded by ADR-0012", "read ADR-0012 for the constraint"),
        ("- Status: Rejected", "no replacement is named on it"),
        ("- Status: Deprecated 2026-10-14", "no replacement is named on it"),
    ],
)
def test_a_historical_records_empty_glob_is_reported_not_a_failure(
    repo, status_line, expected
):
    """A superseding decision is free to delete the paths the old one governed.

    The old record stays, with a pointer on top -- that is what the revision
    protocol asks for. Holding its glob to a live match makes every later run
    exit 2 on a record doing exactly the right thing, and the repairs it then
    asks for are rewrites of history.
    """
    adr(
        repo,
        "0008-old.md",
        f'---\napplies_to:\n  - "src/retired/**"\n---\n\n# 8. Old\n\n{status_line}\n',
    )
    commit(repo, "a superseded record whose paths are gone")

    r = scope(repo, "--changed", "HEAD", "--quiet")
    assert "HISTORICAL: 0008-old.md" in r.stderr
    assert expected in r.stderr
    assert "0008-old.md 'src/retired/**' matches no tracked file" not in r.stderr
    # 0005 is still a live record with a broken glob, so the run still fails --
    # on that one alone.
    assert r.returncode == 2
    assert "0005-nope.md" in r.stderr


@requires_git
def test_a_historical_record_that_still_matches_is_labelled_with_its_successor(repo):
    """It is still printed: the protocol has a reader follow the chain rather
    than stop at whichever record they found first. Labelled, because taking a
    constraint from here implements a decision that was replaced."""
    adr(
        repo,
        "0009-old-web.md",
        '---\napplies_to:\n  - "src/web/**"\n---\n\n'
        "# 9. Old web\n\n- Status: Superseded by ADR-0012\n",
    )
    commit(repo, "a superseded record that still matches")

    r = scope(repo, "--paths", "src/web/**", "--quiet")
    assert "governs this change: 0009-old-web.md -- Superseded by ADR-0012;" in r.stdout
    assert "read ADR-0012 for the constraint in force." in r.stdout
    # The record in force is reported plainly, with no pointer bolted on.
    assert "governs this change: 0002-web.md\n" in r.stdout


@requires_git
def test_sweep_does_not_demand_a_live_glob_from_a_historical_record(repo):
    """`--sweep` is the mode whose empty match is a verdict, so it is the one
    that would have turned another project's retired record into a repair."""
    adr(
        repo,
        "0010-retired.md",
        '---\napplies_to:\n  - "src/gone/**"\n---\n'
        "<!-- applies_to: src/gone/** is created by this work -->\n\n"
        "# 10. Retired\n\n- Status: Superseded by ADR-0012\n",
    )
    commit(repo, "a retired record carrying a forward note")

    r = scope(repo, "--sweep", "--quiet")
    assert "HISTORICAL: 0010-retired.md" in r.stderr
    assert "WRONG GLOB: 0010-retired.md" not in r.stderr
    # 0004's note is still live and still wrong, so sweep still exits 3.
    assert r.returncode == 3
    assert "WRONG GLOB: 0004-notify.md" in r.stderr


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
