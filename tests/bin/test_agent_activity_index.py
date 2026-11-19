"""Tests for bin/agent_activity_index.py.

The GitHub API is never reached: classification and aggregation run on literal
PR dicts, and the history download talks to a throwaway local HTTP server.
Needs cyclopts importable, which the script itself declares inline:

    uv run --no-project --with cyclopts==5.0.0 python -m unittest discover -s tests/bin
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from typing import ClassVar

SCRIPT = Path(__file__).resolve().parents[2] / "bin/agent_activity_index.py"
spec = importlib.util.spec_from_file_location("agent_activity_index", SCRIPT)
aai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aai)


def pr(
    author="alice",
    typename="User",
    branch="feature",
    body="",
    commits=(),
    reviews=(),
    merged=True,
    state="MERGED",
):
    return {
        "number": 1,
        "createdAt": "2026-10-02T00:00:00Z",
        "state": state,
        "mergedAt": "2026-10-03T00:00:00Z" if merged else None,
        "headRefName": branch,
        "body": body,
        "author": {"login": author, "__typename": typename},
        "reviews": {
            "nodes": [
                {"author": {"login": a, "__typename": t}, "state": s}
                for a, t, s in reviews
            ]
        },
        "commits": {"nodes": [{"commit": {"message": m}} for m in commits]},
    }


class ClassifyTests(unittest.TestCase):
    def test_plain_human(self):
        self.assertEqual(aai.classify(pr()), ("human", "alice", set()))

    def test_listed_bot(self):
        self.assertEqual(aai.classify(pr("renovate", "Bot"))[0], "bot")
        self.assertEqual(aai.classify(pr("odlbot", "User"))[0], "bot")

    def test_unlisted_github_app_is_a_bot_not_a_person(self):
        self.assertEqual(
            aai.classify(pr("new-thing[bot]", "Bot"))[:2], ("bot", "new-thing")
        )

    def test_agent_account_is_agent_even_though_it_is_a_bot_type(self):
        self.assertEqual(
            aai.classify(pr("Copilot", "Bot")), ("agent", "copilot", {"Copilot"})
        )

    def test_branch_prefix_body_footer_and_trailer(self):
        self.assertEqual(aai.classify(pr(branch="cc/fix"))[2], {"Claude Code"})
        self.assertEqual(
            aai.classify(
                pr(body="Generated with [Claude Code](https://claude.com/claude-code)")
            )[2],
            {"Claude Code"},
        )
        self.assertEqual(
            aai.classify(
                pr(
                    commits=[
                        "fix\n\nCo-Authored-By: Claude Sonnet <noreply@anthropic.com>"
                    ]
                )
            )[2],
            {"Claude Code"},
        )
        self.assertEqual(
            aai.classify(pr(commits=["x\n\nCo-authored-by: Copilot <c@x>"]))[0],
            "human+agent",
        )

    def test_a_bare_mention_of_an_agent_is_not_a_fingerprint(self):
        item = pr(
            body="Docs for the Claude Code skill and the Copilot setup",
            commits=["document Claude Code usage"],
        )
        self.assertEqual(aai.classify(item)[0], "human")

    def test_a_pasted_link_to_claude_code_is_not_a_footer(self):
        self.assertEqual(
            aai.classify(pr(body="See https://claude.com/claude-code for setup"))[0],
            "human",
        )

    def test_a_person_whose_login_matches_an_agent_is_a_person(self):
        self.assertEqual(aai.classify(pr("claude", "User"))[0], "human")

    def test_body_and_first_commit_are_not_run_together(self):
        item = pr(body="ends with Co-authored-by:", commits=["copilot says hi"])
        self.assertEqual(aai.classify(item)[0], "human")

    def test_bot_wins_over_agent_text(self):
        self.assertEqual(
            aai.classify(pr("renovate", "Bot", body="Generated with Claude Code"))[0],
            "bot",
        )

    def test_ghost_author(self):
        item = pr()
        item["author"] = None
        self.assertEqual(aai.classify(item)[:2], ("human", "ghost"))


class ReviewTests(unittest.TestCase):
    def test_listed_and_unlisted_bots_count_humans_do_not(self):
        item = pr(
            reviews=[
                ("copilot-pull-request-reviewer", "Bot", "COMMENTED"),
                ("renovate-approve", "User", "APPROVED"),
                ("mystery[bot]", "Bot", "COMMENTED"),
                ("bob", "User", "APPROVED"),
            ]
        )
        self.assertEqual(
            [r["bot"] for r in aai.bot_reviews(item)],
            ["Copilot", "Renovate (auto-approve)", "mystery"],
        )

    def test_deleted_reviewer_is_skipped(self):
        item = pr()
        item["reviews"]["nodes"] = [{"author": None, "state": "APPROVED"}]
        self.assertEqual(aai.bot_reviews(item), [])


class MonthTests(unittest.TestCase):
    def test_bounds_including_leap_february_and_december(self):
        self.assertEqual(
            aai.month_bounds("2028-02"), (date(2028, 2, 1), date(2028, 2, 29))
        )
        self.assertEqual(
            aai.month_bounds("2026-12"), (date(2026, 12, 1), date(2026, 12, 31))
        )

    def test_previous_month_crosses_the_year(self):
        self.assertEqual(aai.previous_month(date(2027, 1, 1)), "2026-12")
        self.assertEqual(aai.previous_month(date(2026, 11, 19)), "2026-10")

    def test_months_between_crosses_the_year_and_is_inclusive(self):
        self.assertEqual(
            aai.months_between("2026-11", "2027-02"),
            ["2026-11", "2026-12", "2027-01", "2027-02"],
        )
        self.assertEqual(aai.months_between("2026-03", "2026-03"), ["2026-03"])


class PlanTests(unittest.TestCase):
    def test_a_skipped_month_is_filled_by_the_next_run(self):
        self.assertEqual(
            aai.months_to_compute(["2026-09", "2026-10"], "2026-12", None),
            ["2026-11", "2026-12"],
        )

    def test_a_normal_run_adds_one_month(self):
        self.assertEqual(
            aai.months_to_compute(["2026-09"], "2026-10", None), ["2026-10"]
        )

    def test_rerunning_the_current_month_recomputes_it(self):
        self.assertEqual(
            aai.months_to_compute(["2026-10"], "2026-10", None), ["2026-10"]
        )

    def test_first_run_backfills_from_the_default(self):
        self.assertEqual(
            aai.months_to_compute([], "2026-03", None),
            ["2026-01", "2026-02", "2026-03"],
        )

    def test_explicit_backfill_wins_and_must_not_be_after_the_month(self):
        self.assertEqual(
            aai.months_to_compute(["2026-10"], "2026-10", "2026-08"),
            ["2026-08", "2026-09", "2026-10"],
        )
        with self.assertRaises(SystemExit):
            aai.months_to_compute(["2026-10"], "2026-10", "2026-12")

    def test_months_must_be_yyyy_mm(self):
        for bad in ("2026-1", "2026-13", "2026-00", "../x", "2026-W01", ""):
            with self.assertRaises(SystemExit, msg=bad):
                aai.check_month(bad)
        self.assertEqual(aai.check_month("2026-10"), "2026-10")


class AggregateTests(unittest.TestCase):
    def test_every_pr_lands_in_exactly_one_class(self):
        prs = {
            "r1": [
                pr(),
                pr(branch="cc/x"),
                pr("renovate", "Bot", merged=False, state="CLOSED"),
            ],
            "r2": [
                pr(
                    "Copilot",
                    "Bot",
                    reviews=[("sentry", "Bot", "COMMENTED")],
                    merged=False,
                    state="OPEN",
                )
            ],
        }
        snap = aai.aggregate(
            "2026-10",
            date(2026, 10, 1),
            date(2026, 10, 31),
            prs,
            {"AGENTS.md": ["r1"]},
            5,
        )
        self.assertEqual(snap["total_prs"], 4)
        self.assertEqual(sum(sum(o.values()) for o in snap["classes"].values()), 4)
        self.assertEqual(snap["classes"]["bot"], {"merged": 0, "closed": 1, "open": 0})
        self.assertEqual(
            snap["classes"]["agent"], {"merged": 0, "closed": 0, "open": 1}
        )
        self.assertEqual(snap["by_repo"]["r1"]["human+agent"], 1)
        self.assertEqual(snap["reviewed_by_class"]["agent"], {"prs": 1, "reviewed": 1})
        self.assertEqual(
            snap["bot_reviews"]["Sentry"], {"reviews": 1, "prs": 1, "approvals": 0}
        )
        self.assertEqual(
            snap["agent_prs"]["Claude Code"], {"merged": 1, "closed": 0, "open": 0}
        )


class _Site(BaseHTTPRequestHandler):
    files: ClassVar[dict[str, bytes]] = {}
    fail: ClassVar[set[str]] = set()

    def do_GET(self):
        name = self.path.split("?")[0].rsplit("/", 1)[-1]
        if name in self.fail:
            self.send_response(500)
            self.end_headers()
        elif name in self.files:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.files[name])
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class FetchPrsTests(unittest.TestCase):
    def test_a_window_over_the_search_cap_is_split_and_nothing_is_lost(self):
        def fake(_query, **variables):
            start, end = variables["q"].split("created:")[1].split("..")
            days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
            # Only the whole month is over the cap; each half is under it.
            node = {"number": int(start.replace("-", ""))}
            return {
                "data": {
                    "search": {
                        "issueCount": aai.SEARCH_CAP + 500 if days > 16 else 10,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [node],
                    }
                }
            }

        with mock.patch.object(aai, "gh_graphql", fake):
            prs = aai.fetch_prs("r", date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual([p["number"] for p in prs], [20261001, 20261017])


class DownloadHistoryTests(unittest.TestCase):
    def setUp(self):
        _Site.files, _Site.fail = {}, set()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/agent-activity/"
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "data"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_first_run_is_a_clean_404_and_starts_empty(self):
        self.assertEqual(aai.download_history(self.url, self.dir), [])

    def test_existing_history_is_downloaded(self):
        _Site.files = {
            "index.json": b'{"months": ["2026-09", "2026-10"]}',
            "2026-09.json": b'{"month": "2026-09"}',
            "2026-10.json": b'{"month": "2026-10"}',
        }
        self.assertEqual(
            aai.download_history(self.url, self.dir), ["2026-09", "2026-10"]
        )
        self.assertTrue((self.dir / "2026-10.json").exists())

    def test_server_error_aborts_instead_of_starting_empty(self):
        _Site.fail = {"index.json"}
        with self.assertRaises(SystemExit):
            aai.download_history(self.url, self.dir)

    def test_a_listed_month_that_is_missing_aborts(self):
        _Site.files = {"index.json": b'{"months": ["2026-09"]}'}
        with self.assertRaises(SystemExit):
            aai.download_history(self.url, self.dir)

    def test_a_month_name_that_is_not_a_month_aborts(self):
        _Site.files = {"index.json": b'{"months": ["../../evil"]}'}
        with self.assertRaises(SystemExit):
            aai.download_history(self.url, self.dir)

    def test_a_file_that_describes_another_month_aborts(self):
        _Site.files = {
            "index.json": b'{"months": ["2026-09"]}',
            "2026-09.json": b'{"month": "2026-08"}',
        }
        with self.assertRaises(SystemExit):
            aai.download_history(self.url, self.dir)

    def test_unreachable_site_aborts(self):
        self.server.shutdown()
        self.server.server_close()  # close the listening socket, or the connect hangs until the timeout
        with self.assertRaises(SystemExit):
            aai.download_history(self.url, self.dir)


class BuildSiteTests(unittest.TestCase):
    def test_page_embeds_every_month_and_cannot_be_broken_by_script_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            data = out / "data"
            data.mkdir()
            for month in ("2026-09", "2026-10"):
                (data / f"{month}.json").write_text(
                    json.dumps({"month": month, "note": "</script><b>"})
                )
            aai.build_site(data, out)
            html = (out / "index.html").read_text()
            self.assertEqual(
                json.loads((data / "index.json").read_text()),
                {"months": ["2026-09", "2026-10"]},
            )
            self.assertIn('"month": "2026-10"', html)
            self.assertNotIn("</script><b>", html)

    def test_data_kept_elsewhere_is_published_with_the_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, out = Path(tmp) / "history", Path(tmp) / "site"
            data.mkdir()
            out.mkdir()
            (data / "2026-10.json").write_text(json.dumps({"month": "2026-10"}))
            aai.build_site(data, out)
            self.assertEqual(
                json.loads((out / "data/index.json").read_text()),
                {"months": ["2026-10"]},
            )
            self.assertTrue((out / "data/2026-10.json").exists())


if __name__ == "__main__":
    unittest.main()
