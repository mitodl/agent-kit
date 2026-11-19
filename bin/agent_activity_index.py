#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["cyclopts==5.0.0"]
# ///
"""Agent Activity Index for the public mitodl repositories.

Classifies every pull request opened in a month by *fingerprint* and keeps the
counts, so the published report can show a trend. An agent that leaves no
fingerprint is not counted: every figure is a floor.

  author class   human | human+agent | agent | bot
  agent          author app, body footer, branch prefix, or commit trailer
  bot            renovate, pre-commit-ci, odlbot, ... and any other GitHub App
                 account, which is counted as a bot rather than as a person
  bot review     a review left by a bot account (AI reviewers, Renovate's
                 auto-approve account, and any other bot)

WHY THE DEPENDENCIES ARE DECLARED INLINE. Every publishable package here
releases to PyPI when its own ``pyproject.toml`` changes on main, and
``uv.lock`` records the workspace. A script that added a dependency to either
would be a release trigger. PEP 723 metadata keeps this script out of both.

WHERE THE HISTORY LIVES. Nowhere but the deployed Pages site. ``update``
downloads the earlier months' JSON from the live site, adds the new month,
rebuilds the page and the workflow redeploys all of it. History is therefore
only as durable as the last deploy, which is why the download is guarded: a
clean 404 on the first run starts empty and backfills, while ANY other failure
aborts rather than publish a site that has forgotten its history. A lost history
is recoverable with ``update --backfill-from YYYY-MM``, because every month can
be recomputed from the GitHub API.

    ./bin/agent_activity_index.py snapshot --month 2026-10 --out snap.json
    ./bin/agent_activity_index.py update --out pages/agent-activity --site-url ''
"""

import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import cyclopts

app = cyclopts.App(help="Agent Activity Index for the public mitodl repositories.")

ORG = "mitodl"
SITE_URL = "https://mitodl.github.io/agent-kit/agent-activity/"
DEFAULT_BACKFILL_FROM = "2026-01"
TEMPLATE = Path(__file__).with_name("agent_activity_report.html")
CLASSES = ("human", "human+agent", "agent", "bot")
# The search API returns at most 1000 results per query.
SEARCH_CAP = 1000
# shortcut: reviews(first: 50) and commits(first: 100) are not paginated, so a PR
# with more of either is undercounted. Fine for a floor, revisit if it matters.

# shortcut: these lists are maintained by hand. Any other GitHub App account is
# still counted as a bot via its account type, but only listed names are
# recognised as an agent.
BOT_AUTHORS = {
    "renovate",
    "dependabot",
    "pre-commit-ci",
    "autofix-ci",
    "github-actions",
    "odlbot",
}
AGENT_LOGINS = {
    "copilot-swe-agent": "Copilot",
    "copilot": "Copilot",
    "devin-ai-integration": "Devin",
    "claude": "Claude Code",
    "cursor": "Cursor",
    "google-labs-jules": "Jules",
    "codex": "Codex",
}
# agent -> regex searched in the PR body and in commit messages. Each one matches a
# footer or a Co-authored-by trailer an agent writes, never a bare mention of the
# name, or a human PR that documents "the Claude Code skill" would count as agent work.
TEXT_PATTERNS = {
    "Claude Code": r"Generated with \[?Claude Code|Co-Authored-By: Claude",
    "Codex": r"Co-authored-by: .*codex|chatgpt-codex|Generated with Codex",
    "Copilot": r"Co-authored-by: .*copilot",
    "Gemini": r"Co-authored-by: .*gemini|Generated with Gemini",
    "Cursor": r"Co-authored-by: .*cursor|Generated with Cursor",
}
BRANCH_PREFIXES = {
    "claude/": "Claude Code",
    "cc/": "Claude Code",
    "codex/": "Codex",
    "copilot/": "Copilot",
    "cursor/": "Cursor",
    "jules/": "Jules",
}
BOT_REVIEWERS = {
    "coderabbitai": "CodeRabbit",
    "copilot-pull-request-reviewer": "Copilot",
    "copilot-swe-agent": "Copilot",
    "gemini-code-assist": "Gemini",
    "claude": "Claude",
    "cursor": "Cursor",
    "devin-ai-integration": "Devin",
    "sentry": "Sentry",
    "renovate-approve": "Renovate (auto-approve)",
    "greptile-apps": "Greptile",
    "sourcery-ai": "Sourcery",
}
INSTRUCTION_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".github/copilot-instructions.md",
    ".cursorrules",
    ".cursor/rules",
    ".windsurfrules",
    ".clinerules",
    ".junie/guidelines.md",
]

PR_QUERY = """
query($q: String!, $after: String) {
  rateLimit { remaining resetAt }
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number createdAt state mergedAt headRefName body
      author { login __typename }
      reviews(first: 50) { nodes { author { login __typename } state } }
      commits(first: 100) { nodes { commit { message } } }
    } }
  }
}"""

# Sleep through the rest of the hour rather than fail halfway through a backfill.
RATE_LIMIT_FLOOR = 100


def log(message: str) -> None:
    print(message, file=sys.stderr)


# ── GitHub access ────────────────────────────────────────────────────────────


def gh_graphql(query: str, **variables: str | None) -> dict[str, Any]:
    """Run one GraphQL query through `gh`, retrying transient failures."""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is not None:
            cmd += ["-f", f"{key}={value}"]
    for attempt in range(4):
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            result = json.loads(proc.stdout)
            if "errors" not in result:
                wait_for_rate_limit(result["data"].get("rateLimit"))
                return result
            detail = json.dumps(result["errors"])
        else:
            detail = proc.stderr.strip()
        if attempt == 3:
            msg = f"gh api graphql failed: {detail}"
            raise RuntimeError(msg)
        time.sleep(2**attempt * 5)
    raise AssertionError  # unreachable


def wait_for_rate_limit(rate: dict[str, Any] | None) -> None:
    if not rate or rate["remaining"] > RATE_LIMIT_FLOOR:
        return
    reset = datetime.fromisoformat(rate["resetAt"])
    pause = max(0.0, (reset - datetime.now(UTC)).total_seconds()) + 5
    log(
        f"rate limit low ({rate['remaining']} left); waiting {pause:.0f}s for the reset"
    )
    time.sleep(pause)


def public_repos() -> list[str]:
    out = subprocess.run(
        [
            "gh",
            "repo",
            "list",
            ORG,
            "--visibility",
            "public",
            "--no-archived",
            "--limit",
            "1000",
            "--json",
            "name",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(r["name"] for r in json.loads(out))


def pr_counts(repos: list[str], start: date, end: date) -> dict[str, int]:
    """PRs opened per repo in [start, end], 20 repos per query."""
    counts: dict[str, int] = {}
    for i in range(0, len(repos), 20):
        batch = repos[i : i + 20]
        aliases = "\n".join(
            f'r{j}: search(query: "repo:{ORG}/{name} type:pr created:{start}..{end}", '
            f"type: ISSUE, first: 1) {{ issueCount }}"
            for j, name in enumerate(batch)
        )
        data = gh_graphql(f"query {{ rateLimit {{ remaining resetAt }} {aliases} }}")[
            "data"
        ]
        counts.update(
            {name: data[f"r{j}"]["issueCount"] for j, name in enumerate(batch)}
        )
    return counts


def fetch_prs(repo: str, start: date, end: date) -> list[dict[str, Any]]:
    """Every PR opened in [start, end], splitting the window when search would truncate it."""
    prs: list[dict[str, Any]] = []
    after = None
    first_page = True
    while True:
        res = gh_graphql(
            PR_QUERY, q=f"repo:{ORG}/{repo} type:pr created:{start}..{end}", after=after
        )
        search = res["data"]["search"]
        if first_page and search["issueCount"] > SEARCH_CAP:
            if start == end:
                log(
                    f"{repo}: more than {SEARCH_CAP} PRs on {start}; counting the first {SEARCH_CAP}"
                )
            else:
                mid = start + (end - start) // 2
                return fetch_prs(repo, start, mid) + fetch_prs(
                    repo, mid + timedelta(days=1), end
                )
        first_page = False
        prs += [n for n in search["nodes"] if n]
        if not search["pageInfo"]["hasNextPage"]:
            return prs
        after = search["pageInfo"]["endCursor"]


def instruction_files(repos: list[str]) -> dict[str, list[str]]:
    """Which agent instruction files exist at HEAD, as {path: [repo, ...]}."""
    found: dict[str, list[str]] = {}
    for i in range(0, len(repos), 10):
        batch = repos[i : i + 10]
        files = " ".join(
            f'f{k}: object(expression: "HEAD:{p}") {{ id }}'
            for k, p in enumerate(INSTRUCTION_FILES)
        )
        aliases = "\n".join(
            f'r{j}: repository(owner: "{ORG}", name: "{n}") {{ {files} }}'
            for j, n in enumerate(batch)
        )
        data = gh_graphql(f"query {{ rateLimit {{ remaining resetAt }} {aliases} }}")[
            "data"
        ]
        for j, name in enumerate(batch):
            for k, path in enumerate(INSTRUCTION_FILES):
                if (data[f"r{j}"] or {}).get(f"f{k}"):
                    found.setdefault(path, []).append(name)
    return found


# ── Classification ───────────────────────────────────────────────────────────


def norm(login: str | None) -> str:
    return (login or "ghost").lower().removesuffix("[bot]")


def classify(pr: dict[str, Any]) -> tuple[str, str, set[str]]:
    """Return (author_class, author_login, agents)."""
    author = pr.get("author") or {}
    login = norm(author.get("login"))
    if login in AGENT_LOGINS and author.get("__typename") == "Bot":
        return "agent", login, {AGENT_LOGINS[login]}
    if login in BOT_AUTHORS or author.get("__typename") == "Bot":
        return "bot", login, set()
    agents = {
        agent
        for prefix, agent in BRANCH_PREFIXES.items()
        if pr["headRefName"].startswith(prefix)
    }
    text = "\n".join(
        [
            pr.get("body") or "",
            *(c["commit"]["message"] for c in pr["commits"]["nodes"]),
        ]
    )
    agents |= {
        a for a, rx in TEXT_PATTERNS.items() if re.search(rx, text, re.IGNORECASE)
    }
    return ("human+agent" if agents else "human"), login, agents


def bot_reviews(pr: dict[str, Any]) -> list[dict[str, str]]:
    """Every review left by a bot account, as [{bot, state}]."""
    reviews = []
    for review in pr["reviews"]["nodes"]:
        author = review.get("author")
        if not author:
            continue
        login = norm(author["login"])
        name = BOT_REVIEWERS.get(login) or (
            login if author.get("__typename") == "Bot" else None
        )
        if name:
            reviews.append({"bot": name, "state": review["state"]})
    return reviews


def outcome(pr: dict[str, Any]) -> str:
    return "merged" if pr["mergedAt"] else pr["state"].lower()


def tally(outcomes: list[str]) -> dict[str, int]:
    counts = Counter(outcomes)
    return {k: counts.get(k, 0) for k in ("merged", "closed", "open")}


def aggregate(
    month: str,
    start: date,
    end: date,
    prs_by_repo: dict[str, list[dict[str, Any]]],
    files: dict[str, list[str]],
    repo_count: int,
) -> dict[str, Any]:
    """Reduce a month of PRs to counts. Nothing per-person or per-PR is kept."""
    rows = []
    for repo, prs in prs_by_repo.items():
        for pr in prs:
            cls, author, agents = classify(pr)
            rows.append(
                {
                    "repo": repo,
                    "class": cls,
                    "author": author,
                    "agents": agents,
                    "outcome": outcome(pr),
                    "reviews": bot_reviews(pr),
                    "trailers": Counter(
                        a
                        for c in pr["commits"]["nodes"]
                        for a, rx in TEXT_PATTERNS.items()
                        if cls != "bot"
                        and re.search(rx, c["commit"]["message"], re.IGNORECASE)
                    ),
                }
            )
    agents = sorted({a for r in rows for a in r["agents"]})
    bots = sorted({r["author"] for r in rows if r["class"] == "bot"})
    reviewers = sorted({x["bot"] for r in rows for x in r["reviews"]})
    return {
        "month": month,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_prs": len(rows),
        "repos_with_prs": len(prs_by_repo),
        "repo_count": repo_count,
        "classes": {
            c: tally([r["outcome"] for r in rows if r["class"] == c]) for c in CLASSES
        },
        "agent_prs": {
            a: tally([r["outcome"] for r in rows if a in r["agents"]]) for a in agents
        },
        "commit_trailers": dict(
            sum((r["trailers"] for r in rows), Counter()).most_common()
        ),
        "bot_authors": {
            b: tally(
                [r["outcome"] for r in rows if r["author"] == b and r["class"] == "bot"]
            )
            for b in bots
        },
        "bot_reviews": {
            b: {
                "reviews": sum(x["bot"] == b for r in rows for x in r["reviews"]),
                "prs": sum(any(x["bot"] == b for x in r["reviews"]) for r in rows),
                "approvals": sum(
                    x["bot"] == b and x["state"] == "APPROVED"
                    for r in rows
                    for x in r["reviews"]
                ),
            }
            for b in reviewers
        },
        "reviewed_by_class": {
            c: {
                "prs": sum(r["class"] == c for r in rows),
                "reviewed": sum(r["class"] == c and bool(r["reviews"]) for r in rows),
            }
            for c in CLASSES
        },
        "by_repo": {
            repo: {
                c: sum(r["repo"] == repo and r["class"] == c for r in rows)
                for c in CLASSES
            }
            for repo in sorted({r["repo"] for r in rows})
        },
        "instruction_files": files,
    }


# ── Months ───────────────────────────────────────────────────────────────────


MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def check_month(month: str) -> str:
    if not MONTH_RE.match(month):
        msg = f"not a YYYY-MM month: {month!r}"
        raise SystemExit(msg)
    return month


def next_month(month: str) -> str:
    return months_between(month, "9999-12")[1]


def months_to_compute(
    history: list[str], month: str, backfill_from: str | None
) -> list[str]:
    """Which months a run must compute.

    An explicit backfill recomputes from there. Otherwise a run fills every month
    after the newest one the site holds, so a run that failed on the 1st leaves a gap
    for only one month, not for good. A first run (no history) backfills from 2026-01.
    """
    if backfill_from is None:
        backfill_from = next_month(max(history)) if history else DEFAULT_BACKFILL_FROM
        backfill_from = min(backfill_from, month)
    if backfill_from > month:
        msg = (
            f"--backfill-from {backfill_from} is after the month being added ({month})"
        )
        raise SystemExit(msg)
    return months_between(backfill_from, month)


def month_bounds(month: str) -> tuple[date, date]:
    first = date.fromisoformat(f"{month}-01")
    return first, (first.replace(day=28) + timedelta(days=4)).replace(
        day=1
    ) - timedelta(days=1)


def previous_month(today: date) -> str:
    last = today.replace(day=1) - timedelta(days=1)
    return f"{last.year}-{last.month:02d}"


def months_between(first: str, last: str) -> list[str]:
    months, (y, m) = [], (int(first[:4]), int(first[5:]))
    while f"{y}-{m:02d}" <= last:
        months.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def snapshot_month(
    month: str, only: list[str] | None, files: dict[str, list[str]], repos: list[str]
) -> dict[str, Any]:
    start, end = month_bounds(month)
    log(f"{month}: counting PRs in {len(only or repos)} repos")
    counts = pr_counts(only or repos, start, end)
    prs_by_repo = {}
    for repo in sorted(r for r, n in counts.items() if n):
        log(f"{month}: fetching {repo} ({counts[repo]} PRs)")
        prs_by_repo[repo] = fetch_prs(repo, start, end)
    return aggregate(month, start, end, prs_by_repo, files, len(repos))


# ── History and site ─────────────────────────────────────────────────────────


def download_history(site_url: str, data_dir: Path) -> list[str]:
    """Fetch the months the live site already holds.

    A clean 404 for index.json is the first run and returns []. Everything else
    raises: publishing after a failed download would overwrite real history with
    an empty or partial site.
    """
    base = site_url.rstrip("/") + "/data/"

    def fetch(name: str) -> bytes:
        try:
            # Pages serves max-age=600, so a run within ten minutes of a deploy can read the
            # index from before it. months_to_compute refills a missing month when the run
            # asks for the default month, but an explicit older month would redeploy without
            # the newer one. The workflow waits that window out before calling this.
            with urllib.request.urlopen(base + name, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            if err.code == 404 and name == "index.json":
                raise FileNotFoundError from err
            msg = f"could not download {base}{name}: HTTP {err.code}. Refusing to publish without the existing history."
            raise SystemExit(msg) from err
        except OSError as err:
            msg = f"could not download {base}{name}: {err}. Refusing to publish without the existing history."
            raise SystemExit(msg) from err

    try:
        months = json.loads(fetch("index.json"))["months"]
    except FileNotFoundError:
        return []
    data_dir.mkdir(parents=True, exist_ok=True)
    for month in months:
        check_month(month)  # it becomes a file name and a URL
        body = fetch(f"{month}.json")
        if json.loads(body).get("month") != month:
            msg = (
                f"{base}{month}.json describes a different month. Refusing to publish."
            )
            raise SystemExit(msg)
        (data_dir / f"{month}.json").write_bytes(body)
    return months


def build_site(data_dir: Path, out: Path) -> None:
    snapshots = [
        json.loads(p.read_text()) for p in sorted(data_dir.glob("????-??.json"))
    ]
    (data_dir / "index.json").write_text(
        json.dumps({"months": [s["month"] for s in snapshots]})
    )
    # The deployed site is `out`, and the next run downloads its history from
    # `out/data`, so the data must be published with the page.
    if (out / "data").resolve() != data_dir.resolve():
        shutil.copytree(data_dir, out / "data", dirs_exist_ok=True)
    payload = json.dumps(snapshots).replace("</", "<\\/")
    (out / "index.html").write_text(TEMPLATE.read_text().replace("__DATA__", payload))


# ── Commands ─────────────────────────────────────────────────────────────────


@app.command
def snapshot(
    out: Path,
    month: str | None = None,
    repo: Annotated[
        list[str] | None, cyclopts.Parameter(help="Limit to these repos; repeatable.")
    ] = None,
) -> None:
    """Write one month's counts to a JSON file. Defaults to last month."""
    month = check_month(month or previous_month(datetime.now(UTC).date()))
    repos = public_repos()
    out.write_text(
        json.dumps(
            snapshot_month(month, repo, instruction_files(repos), repos), indent=2
        )
    )
    log(f"wrote {out}")


@app.command
def site(data_dir: Path, out: Path) -> None:
    """Build the page from a directory of monthly JSON files."""
    out.mkdir(parents=True, exist_ok=True)
    build_site(data_dir, out)


@app.command
def update(
    out: Path,
    month: str | None = None,
    site_url: str = SITE_URL,
    backfill_from: str | None = None,
) -> None:
    """Add a month to the published history and rebuild the page.

    Fills every month after the newest one already published, so one failed run
    leaves no permanent gap. First run (no site yet) backfills from 2026-01. Pass
    --backfill-from to recompute a range, for example to recover lost history.
    Pass --site-url '' to skip the download for a local run.
    """
    month = check_month(month or previous_month(datetime.now(UTC).date()))
    if backfill_from is not None:
        check_month(backfill_from)
    data_dir = out / "data"
    out.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(exist_ok=True)
    history = download_history(site_url, data_dir) if site_url else []
    to_compute = months_to_compute(history, month, backfill_from)
    repos = public_repos()
    files = instruction_files(repos)
    for m in to_compute:
        (data_dir / f"{m}.json").write_text(
            json.dumps(snapshot_month(m, None, files, repos))
        )
    build_site(data_dir, out)
    log(
        f"site written to {out} with months {json.loads((data_dir / 'index.json').read_text())['months']}"
    )


if __name__ == "__main__":
    app()
