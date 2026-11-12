"""The plan comment is rendered, not written, so its renderer is testable.

`plan-comment.jq` is a pure function from the work graph to the comment body.
These exercise it directly with fixtures, which is why they run without
omnigraph: the failure modes that matter -- a disabled PR lookup rendering as
"nothing opened yet", a blocked chunk losing its blocker -- are all in the
formatting, not in the fetch.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

JQ_PROGRAM = (
    Path(__file__).resolve().parent.parent
    / "witan/skills/plan-technical-approach/scripts/plan-comment.jq"
)

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not on PATH")


def render(payload: dict) -> str:
    """Run the renderer over one input object and return the body."""
    result = subprocess.run(
        ["jq", "-r", "-f", str(JQ_PROGRAM)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def chunk(**over):
    """One chunk row in the shape `witan tasks` actually emits.

    Not the server's shape. Rows reach the renderer through `render_table`,
    which normalises every `None` to `""` because TOML has no null -- so an
    unlinked chunk arrives with `external_uri: ""` and `live_pr: ""`, not
    nulls. Defaulting to the server shape here is exactly why the renderer
    shipped unable to read its own input.
    """
    base = {
        "slug": "tk-c1",
        "title": "Static headers",
        "status": "open",
        "created_at": "2026-10-23T01:00:00Z",
        "external_uri": "",
        "blocked_by": "",
        "live_pr": "",
    }
    return {**base, **over}


def payload(chunks, **over):
    return {"live_pr": True, "generated_on": "2026-11-02", "chunks": chunks, **over}


@requires_jq
def test_renders_pr_state_from_the_live_record():
    body = render(
        payload(
            [
                chunk(
                    external_uri="https://github.com/o/r/pull/41",
                    live_pr={
                        "kind": "pr",
                        "ref": "o/r#41",
                        "state": "open",
                        "draft": False,
                        "review_decision": "CHANGES_REQUESTED",
                        "checks": {"pass": 2, "fail": 1, "pending": 0},
                    },
                )
            ]
        )
    )
    assert "[o/r#41](https://github.com/o/r/pull/41)" in body
    assert "open, changes requested, checks failing" in body


@requires_jq
def test_a_disabled_lookup_is_not_reported_as_no_pr():
    """The misreading this guards: a blank state cell on a chunk that HAS a PR
    reads as "nothing opened yet", and someone redoes work already in review.

    `live_pr: false` is the `WITAN_LIVE_PR=0` case, where `annotate` returns
    the row untouched -- so the row's own `live_pr` is absent (or "" once
    `render_table` has been over it), not an error.
    """
    body = render(
        payload(
            [chunk(external_uri="https://github.com/o/r/pull/41")],
            live_pr=False,
        )
    )
    assert "not fetched" in body
    assert "unavailable" not in body


@requires_jq
def test_a_failed_lookup_is_distinct_from_a_disabled_one():
    body = render(
        payload(
            [
                chunk(
                    external_uri="https://github.com/o/r/pull/41",
                    live_pr={"error": "live state lookup failed: gh exited 1"},
                )
            ]
        )
    )
    assert "unavailable" in body
    assert "not fetched" not in body


@requires_jq
def test_blocked_chunks_name_their_blocker_in_the_row_and_the_header():
    body = render(payload([chunk(status="blocked", blocked_by=["tk-decide-d6"])]))
    assert "**Blocked on:** tk-decide-d6" in body
    assert "blocked on tk-decide-d6" in body


@requires_jq
def test_blocked_by_is_accepted_in_both_shapes():
    """`task_list` returns a list; `witan tasks` projects it through
    `", ".join(...)` for its table, so the CLI hands the renderer a STRING.
    Reading only the server shape is what made the first end-to-end render
    die with "Cannot iterate over string".
    """
    as_list = render(payload([chunk(status="blocked", blocked_by=["tk-a", "tk-b"])]))
    as_string = render(payload([chunk(status="blocked", blocked_by="tk-a, tk-b")]))
    assert "**Blocked on:** tk-a, tk-b" in as_list
    assert as_string == as_list

    # An unblocked row's empty string must not become a blocker named "".
    assert "blocked on" not in render(payload([chunk(blocked_by="")]))


@requires_jq
def test_live_pr_is_accepted_in_all_three_shapes():
    """The server omits `live_pr`; `render_table` turns a missing one into "".

    All three mean the same thing -- nobody asked -- so all three must render
    the same. Testing `!= null` and then indexing is what made the first
    end-to-end render die with `Cannot index string with "error"`, on the
    commonest input there is: chunk tasks with no PR yet.
    """
    for uri in ("", "https://github.com/o/r/pull/41"):
        absent = render(
            payload(
                [{k: v for k, v in chunk(external_uri=uri).items() if k != "live_pr"}]
            )
        )
        as_null = render(payload([chunk(external_uri=uri, live_pr=None)]))
        as_empty = render(payload([chunk(external_uri=uri, live_pr="")]))
        assert absent == as_null == as_empty, f"shapes diverge for external_uri={uri!r}"


@requires_jq
def test_a_pipe_in_a_chunk_title_does_not_break_the_table():
    """Titles are authored by whoever planned the work. An unescaped `|` ends
    the cell early and shifts every column after it, so the PR a reader is
    looking for appears under State."""
    body = render(payload([chunk(title="Expand | migrate | contract")]))
    row = next(r for r in body.splitlines() if r.startswith("| 1 |"))
    assert r"Expand \| migrate \| contract" in row
    # Count the pipes that still DELIMIT -- an escaped one does not. Four
    # columns means five delimiters; an unescaped title would give seven.
    assert len(re.findall(r"(?<!\\)\|", row)) == 5, f"escaping failed: {row}"


@requires_jq
def test_chunks_are_numbered_in_creation_order_not_input_order():
    """`plan-technical-approach` creates the chunk tasks in plan order, so
    `created_at` carries the numbering. The server orders by priority and
    `updated_at`, which is not it -- touching chunk 3 must not renumber it to 1.
    """
    body = render(
        payload(
            [
                chunk(slug="tk-c3", title="third", created_at="2026-10-23T03:00:00Z"),
                chunk(slug="tk-c1", title="first", created_at="2026-10-23T01:00:00Z"),
                chunk(slug="tk-c2", title="second", created_at="2026-10-23T02:00:00Z"),
            ]
        )
    )
    rows = [line for line in body.splitlines() if line.startswith("| ")]
    numbered = [r for r in rows if not r.startswith("| # ")]
    assert numbered[0].startswith("| 1 | first ")
    assert numbered[1].startswith("| 2 | second ")
    assert numbered[2].startswith("| 3 | third ")


@requires_jq
def test_optional_sections_leave_no_blank_lines_behind():
    """Work with no spec, no ADRs and no note renders a table and nothing else
    -- not a run of empty lines where the omitted sections were."""
    body = render(payload([chunk()]))
    assert not body.startswith("\n")
    assert "\n\n\n" not in body
    assert "**Note:**" not in body
    assert "**Plan:**" not in body


@requires_jq
def test_the_cli_projection_is_what_the_renderer_reads(capsys):
    """The seam, asserted rather than assumed.

    Every other test here hands the renderer a hand-built fixture, so a fixture
    that drifts from what `witan tasks` emits tests nothing. This one drives the
    real projection -- `render_table` in json mode, which is what
    `--output-format json` reaches -- and feeds its actual stdout to the
    renderer. Nothing asserted this before, which is why the renderer could not
    read its own input while eight tests passed.
    """
    from witan.cli._common import render_table
    from witan.cli.output import set_output_format

    columns = [
        "priority",
        "status",
        "type",
        "slug",
        "title",
        "repo",
        "assignee",
        "blocked_by",
        "external_uri",
        "created_at",
        "live_pr",
        "pr",
    ]
    rows = [
        # An unlinked chunk: `annotate` never ran, so the server-side value is
        # None for both. This is the row that crashed the renderer.
        {
            "priority": "p2",
            "status": "open",
            "type": "feature",
            "slug": "tk-c1",
            "title": "Static headers",
            "repo": "mitodl/mit-learn",
            "assignee": None,
            "blocked_by": "",
            "external_uri": None,
            "created_at": "2026-10-23T01:00:00Z",
            "live_pr": None,
            "pr": "",
        },
    ]

    set_output_format("json")
    try:
        render_table(title="Tasks", columns=columns, rows=rows, empty="none")
        dumped = json.loads(capsys.readouterr().out)
    finally:
        set_output_format("txt")

    # Pin the normalisation itself: if render_table ever stops doing this, the
    # renderer's tolerance is still correct but this test should say why.
    assert dumped["rows"][0]["live_pr"] == "", "render_table no longer normalises None"

    body = render(
        {"live_pr": True, "generated_on": "2026-11-03", "chunks": dumped["rows"]}
    )
    assert "| 1 | Static headers | — | not started |" in body


@requires_jq
def test_the_body_stays_a_status_board():
    """The whole point of generating it. 14080's hand-written comment was
    18,344 characters; a rendered one is bounded by the chunk count."""
    body = render(
        payload(
            [
                chunk(slug=f"tk-c{i}", title=f"chunk {i}", created_at=f"2026-10-2{i}")
                for i in range(1, 7)
            ],
            spec="https://github.com/o/r/blob/main/docs/plans/x-spec.md",
            decisions="[ADR-0014 — a decision](https://example.com/a)",
            note="The bottleneck is review, not work.",
        )
    )
    assert len(body) < 1500, f"rendered {len(body)} chars"
