"""`check-deviation-log.sh` makes the deviation-log protocol checkable.

The rule that matters most is the replan threshold: `implement-plan` says to
"count the entries rather than judging the feel of it", which only works if
something counts. The rest -- six fields, the 800-character cap, newest-first,
append-only -- were prose with nothing behind them.
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "witan/skills/plan-technical-approach/scripts/check-deviation-log.sh"
)

requires_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is needed"
)


def check(path: Path, *args: str, cwd: Path | None = None):
    return subprocess.run(
        ["bash", str(SCRIPT), str(path), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


def entry(
    date="2026-10-26",
    summary="chunk 4 splits into 4a/4b",
    assumed="one CSP policy for the whole site, enforced in one chunk.",
    found="Learn ships its own embed feature, so frame-ancestors differs.",
    changed="two policies from chunk 3; chunk 4 splits.",
    touches="chunks · D7, D8 (spec) · ADR-0014",
    approved="@rhysyngsun",
    pr="https://github.com/mitodl/mit-learn/pull/4183",
    fields=None,
) -> str:
    rows = fields or [
        ("Assumed", assumed),
        ("Found", found),
        ("Changed to", changed),
        ("Touches", touches),
        ("Approved by", approved),
        ("PR", pr),
    ]
    body = "\n".join(f"- **{k}:** {v}" for k, v in rows)
    return f"### {date} — {summary}\n\n{body}\n"


def log(tmp_path: Path, *entries: str, name="x-deviations.md") -> Path:
    p = tmp_path / name
    p.write_text("# x — deviation log\n\n" + "\n".join(entries))
    return p


@requires_bash
def test_a_well_formed_log_passes_and_summarises(tmp_path):
    r = check(log(tmp_path, entry()))
    assert r.returncode == 0, r.stderr
    assert "1 entry, 1 of which changed the chunk graph's shape." in r.stdout
    assert "ADR-0014" in r.stdout


@requires_bash
def test_the_replan_threshold_is_counted_not_felt(tmp_path):
    """Three entries that changed the chunk graph's SHAPE, not three entries.

    The distinction is the whole threshold: a deviation that moved a decision
    is repairable, one that moved the delivery is what accumulates until you
    are patching a plan you no longer have.
    """
    shaped = [entry(date=f"2026-10-2{i}", touches="chunks · D1") for i in (8, 7, 6)]
    flat = [entry(date=f"2026-10-2{i}", touches="D2 (spec)") for i in (5, 4, 3)]

    under = check(log(tmp_path, *shaped[:2], *flat))
    assert under.returncode == 0, under.stderr
    assert "2 of which changed the chunk graph's shape" in under.stdout
    assert "REPLAN" not in under.stderr

    at = check(log(tmp_path, *shaped, *flat, name="y-deviations.md"))
    assert at.returncode == 3
    assert "REPLAN: 3 entries changed the chunk graph's shape" in at.stderr
    assert "rewrites it FROM these entries" in at.stderr


@requires_bash
def test_the_14080_history_reproduces_the_calibration(tmp_path):
    """The threshold was set from this: reconstructing mitodl/hq#14080 as
    entries gives six, of which three changed the chunk graph's shape. If this
    stops reproducing, either the counting or the documented number is wrong.
    """
    entries = [
        entry(date="2026-10-28", touches="chunks · D9"),
        entry(date="2026-10-26", touches="chunks · D7, D8 (spec) · ADR-0014"),
        entry(date="2026-10-24", touches="D6 (spec) · ADR-0014"),
        entry(date="2026-10-23", touches="chunks"),
        entry(date="2026-10-22", touches="D3 (spec)"),
        entry(date="2026-10-21", touches="D1, D2 (spec) · ADR-0014"),
    ]
    r = check(log(tmp_path, *entries))
    assert r.returncode == 3
    assert "6 entries, 3 of which changed the chunk graph's shape." in r.stdout


@requires_bash
def test_a_missing_field_is_named(tmp_path):
    r = check(
        log(
            tmp_path,
            entry(
                fields=[
                    ("Assumed", "a"),
                    ("Found", "b"),
                    ("Changed to", "c"),
                    ("Touches", "D1"),
                    ("PR", "http://x"),
                ]
            ),
        )
    )
    assert r.returncode == 1
    assert "missing field: **Approved by:**" in r.stderr


@requires_bash
def test_fields_out_of_order_are_caught(tmp_path):
    r = check(
        log(
            tmp_path,
            entry(
                fields=[
                    ("Assumed", "a"),
                    ("Found", "b"),
                    ("Changed to", "c"),
                    ("Approved by", "@x"),
                    ("Touches", "D1"),
                    ("PR", "http://x"),
                ]
            ),
        )
    )
    assert r.returncode == 1
    assert "fields are out of order" in r.stderr


@requires_bash
def test_the_800_character_cap_is_enforced(tmp_path):
    """The cap is the point: an entry that does not fit is carrying reasoning,
    and reasoning belongs in the Dn. section or the ADR it changed."""
    ok = check(log(tmp_path, entry(assumed="x" * 400)))
    assert ok.returncode == 0, ok.stderr

    over = check(log(tmp_path, entry(assumed="x" * 820), name="y-deviations.md"))
    assert over.returncode == 1
    assert "the cap is 800" in over.stderr


@requires_bash
def test_the_cap_counts_characters_not_bytes(tmp_path):
    """An entry of em dashes is this house style, not an edge case. Counting
    bytes would refuse a compliant entry at a third of the real length."""
    r = check(log(tmp_path, entry(assumed="—" * 400)))
    assert r.returncode == 0, r.stderr


@requires_bash
def test_entries_must_be_newest_first(tmp_path):
    r = check(log(tmp_path, entry(date="2026-10-21"), entry(date="2026-10-26")))
    assert r.returncode == 1
    assert "newer than the entry above it" in r.stderr


@requires_bash
def test_a_future_date_is_caught(tmp_path):
    """An agent's idea of today is routinely wrong, which is why the format
    mandates `date -u`. A future date is the tell that it guessed."""
    r = check(log(tmp_path, entry(date="2099-01-01")))
    assert r.returncode == 1
    assert "is in the future" in r.stderr


@requires_bash
def test_a_field_spilling_onto_a_second_line_is_caught(tmp_path):
    p = tmp_path / "x-deviations.md"
    p.write_text(
        "# x\n\n"
        + entry().replace(
            "- **Found:**", "- **Found:** first line\n    continued here\n- **Found:**"
        )
    )
    r = check(p)
    assert r.returncode == 1
    assert "continues onto a second line" in r.stderr


@requires_bash
def test_list_adrs_prints_only_the_adrs(tmp_path):
    """This is what complete-project supersedes from -- currently derived by
    eye from a file it is told to read end to end."""
    r = check(
        log(
            tmp_path,
            entry(date="2026-10-26", touches="chunks · ADR-0014"),
            entry(date="2026-10-24", touches="D6 · ADR-0009 · ADR-0014"),
        ),
        "--list-adrs",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["ADR-0009", "ADR-0014"], "sorted and deduped"


@requires_bash
def test_a_missing_log_is_an_error_when_asked_for_by_path(tmp_path):
    """No log at all means no deviations, which is normal -- but a path that
    was asked for and is not there is either wrong or deleted too early."""
    r = check(tmp_path / "nope.md")
    assert r.returncode == 1
    assert "No log means no deviations" in r.stderr


# --- append-only, which only a diff can check --------------------------------


def git(cwd: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", ".")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "t")
    return tmp_path


requires_git = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git and bash are needed",
)


@requires_git
def test_since_accepts_an_appended_entry(repo):
    p = log(repo, entry(date="2026-10-21", touches="D1"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first entry")

    p.write_text(
        "# x — deviation log\n\n"
        + entry(date="2026-10-26", touches="D2")
        + "\n"
        + entry(date="2026-10-21", touches="D1")
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "append")

    r = check(p, "--since", "HEAD~1", cwd=repo)
    assert r.returncode == 0, r.stderr


@requires_git
def test_since_catches_an_edited_entry(repo):
    """Append-only is the rule an agent is likeliest to break by "tidying" an
    entry. A deviation later reversed gets a SECOND entry saying so."""
    p = log(repo, entry(date="2026-10-21", assumed="the original wording"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first entry")

    p.write_text(
        textwrap.dedent(p.read_text()).replace(
            "the original wording", "a tidier wording"
        )
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "tidy it")

    r = check(p, "--since", "HEAD~1", cwd=repo)
    assert r.returncode == 1
    assert "were REMOVED from the log" in r.stderr
    assert "append-only" in r.stderr
