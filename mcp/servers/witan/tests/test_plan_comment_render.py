"""The plan comment is rendered, not written, so its renderer is testable.

`plan-comment.jq` is a pure function from the work graph to the comment body.
These exercise it directly with fixtures, which is why they run without
omnigraph: the failure modes that matter -- a disabled PR lookup rendering as
"nothing opened yet", a blocked chunk losing its blocker -- are all in the
formatting, not in the fetch.
"""

import json
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
    base = {
        "slug": "tk-c1",
        "title": "Static headers",
        "status": "open",
        "created_at": "2026-10-23T01:00:00Z",
        "external_uri": None,
        "blocked_by": [],
        "live_pr": None,
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
    the row untouched -- so the key is absent, not an error.
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
