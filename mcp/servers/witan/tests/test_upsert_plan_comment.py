"""`upsert-plan-comment.sh` is the one script here that writes to GitHub.

It had no test at all, which is how a renderer that could not read its own
input reached a path whose whole job is to post that output. These run it
against a stubbed `gh` in `--dry-run`, so nothing leaves the machine: the
point is the assembly -- render, marker, digest, the refusals -- not the API.

`--dry-run` still performs the (read-only) lookup, because create-vs-update is
the question it exists to answer, so the stub has to answer three calls.
"""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPTS = (
    Path(__file__).resolve().parent.parent
    / "witan/skills/plan-technical-approach/scripts"
)
UPSERT = SCRIPTS / "upsert-plan-comment.sh"

requires_tools = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="jq and bash are needed",
)


def gh_stub(tmp_path: Path, comments: list[dict] | None = None) -> Path:
    """A `gh` that answers the three calls --dry-run makes, and nothing else.

    Anything it does not recognise exits 1 with the argv, so a new call site
    shows up as a loud failure here rather than as a silent network attempt.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    payload = json.dumps([comments or []])
    (bindir / "gh").write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        case "$*" in
          "api user --jq .login")        echo "stub-user" ;;
          *"--jq [.permissions.push"*)   printf 'true\\tfalse\\n' ;;
          *"/comments?per_page=100")     cat <<'JSON'
        {payload}
        JSON
          ;;
          *) echo "gh stub: unhandled: $*" >&2; exit 1 ;;
        esac
        """)
    )
    (bindir / "gh").chmod(0o755)
    return bindir


def run(tmp_path: Path, *args: str, comments: list[dict] | None = None, **kw):
    env = {
        **os.environ,
        "PATH": f"{gh_stub(tmp_path, comments)}{os.pathsep}{os.environ['PATH']}",
        "OL_PLAN_DIR": str(tmp_path / "plan-cache"),
    }
    return subprocess.run(
        ["bash", str(UPSERT), "--repo", "o/r", "--issue", "7", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,  # every refusal below is a nonzero exit under test
        **kw,
    )


def tasks_json(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "rows.json"
    path.write_text(json.dumps({"title": "Tasks", "rows": rows}))
    return path


# The shape `witan tasks --live-pr --output-format json` emits: `render_table`
# has normalised every None to "". Hand-building the SERVER's shape here would
# reproduce the fixture drift that let the renderer ship broken.
ROW = {
    "priority": "p2",
    "status": "open",
    "type": "feature",
    "slug": "tk-c1",
    "title": "Static headers",
    "repo": "o/r",
    "assignee": "",
    "blocked_by": "",
    "external_uri": "",
    "created_at": "2026-10-23T01:00:00Z",
    "live_pr": "",
    "pr": "",
}


@requires_tools
def test_render_dry_run_emits_a_marked_body_with_a_digest(tmp_path):
    r = run(
        tmp_path,
        "--render",
        "--epic",
        "tk-epic",
        "--dry-run",
        "--tasks-json",
        str(tasks_json(tmp_path, [ROW])),
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("<!-- ol-plan:v1 -->"), r.stdout
    assert "<!-- ol-plan:sha:" in r.stdout
    assert "| 1 | Static headers |" in r.stdout
    assert "would POST a new comment" in r.stderr


@requires_tools
def test_an_epic_with_no_children_refuses_rather_than_posting_an_empty_table(tmp_path):
    """A comment whose chunk table is empty reads as "this work has no chunks",
    and posting it would overwrite a correct render with that claim."""
    r = run(
        tmp_path,
        "--render",
        "--epic",
        "tk-epic",
        "--dry-run",
        "--tasks-json",
        str(tasks_json(tmp_path, [])),
    )
    assert r.returncode == 7
    assert "no child tasks" in r.stderr


@requires_tools
def test_expect_sha_is_refused_with_render(tmp_path):
    """It guards hand-written prose. Accepting it on a rendered body would
    imply a protection that is not operating."""
    r = run(
        tmp_path,
        "--render",
        "--epic",
        "tk-epic",
        "--dry-run",
        "--expect-sha",
        "deadbeef",
        "--tasks-json",
        str(tasks_json(tmp_path, [ROW])),
    )
    assert r.returncode == 1
    assert "--expect-sha does not apply to --render" in r.stderr


@requires_tools
def test_an_over_long_note_is_refused_not_truncated(tmp_path):
    r = run(
        tmp_path,
        "--render",
        "--epic",
        "tk-epic",
        "--dry-run",
        "--note",
        "x" * 281,
        "--tasks-json",
        str(tasks_json(tmp_path, [ROW])),
    )
    assert r.returncode == 1
    assert "281 characters" in r.stderr


@requires_tools
def test_require_existing_refuses_to_create_a_second_comment(tmp_path):
    r = run(
        tmp_path,
        "--render",
        "--epic",
        "tk-epic",
        "--dry-run",
        "--require-existing",
        "--tasks-json",
        str(tasks_json(tmp_path, [ROW])),
    )
    assert r.returncode == 5
    assert "no plan comment found" in r.stderr


@requires_tools
def test_an_existing_marked_comment_is_updated_in_place(tmp_path):
    existing = [
        {
            "id": 99,
            "node_id": "n1",
            "user": {"login": "stub-user"},
            "html_url": "https://github.com/o/r/issues/7#issuecomment-99",
            "body": "<!-- ol-plan:v1 -->\n\nold body\n",
        }
    ]
    r = run(
        tmp_path,
        "--render",
        "--epic",
        "tk-epic",
        "--dry-run",
        "--require-existing",
        "--tasks-json",
        str(tasks_json(tmp_path, [ROW])),
        comments=existing,
    )
    assert r.returncode == 0, r.stderr
    assert "would PATCH repos/o/r/issues/comments/99" in r.stderr


@requires_tools
def test_print_draft_path_is_derived_not_taken(tmp_path):
    """The path is derived so the documented invocation stays byte-identical
    across runs -- a permission-allowlist entry approved once keeps matching."""
    r = run(tmp_path, "--print-draft-path")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("o-r-7.md")
