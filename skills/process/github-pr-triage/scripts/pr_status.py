# /// script
# requires-python = ">=3.11"
# dependencies = ["cyclopts>=3"]
# ///
"""Report the status of open pull requests by one author, grouped by whose move it is.

"Feedback not addressed" is read from GitHub's own review-thread state: an
unresolved thread whose last comment is not from the PR author is waiting on
the author. Approval is read from the reviews themselves, because
``reviewDecision`` is empty on repos without required reviews.
"""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Literal

import cyclopts
from cyclopts import Parameter

app = cyclopts.App()

# GitHub's search endpoint times out (HTTP 502) on large pages of this query,
# so details are fetched by node ID in small batches, in parallel.
BATCH_SIZE = 5
MAX_WORKERS = 8
SEARCH_LIMIT = 200

DETAIL_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) { ... on PullRequest {
    number title url isDraft updatedAt reviewDecision mergeable
    author { login }
    repository { nameWithOwner }
    reviewRequests(first: 20) { nodes { requestedReviewer {
      ... on User { login } ... on Bot { login } ... on Team { name } ... on Mannequin { login }
    } } }
    latestOpinionatedReviews(first: 20) { nodes { state submittedAt author { login } } }
    reviews(last: 50, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]) { nodes {
      state submittedAt bodyText author { login __typename }
    } }
    comments(last: 30) { nodes { createdAt author { login } } }
    reviewThreads(first: 100) { totalCount nodes {
      isResolved isOutdated
      first: comments(first: 1) { nodes { author { login } } }
      last: comments(last: 1) { nodes { author { login } } }
    } }
    commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
  } }
}
"""

Group = Literal["ready", "yours", "waiting", "unreviewed", "draft"]
GROUP_TITLES: dict[Group, str] = {
    "ready": "Ready to merge",
    "yours": "Needs you",
    "waiting": "Waiting on others",
    "unreviewed": "No human reviewer yet",
    "draft": "Drafts",
}


@dataclass
class PRStatus:
    repo: str
    number: int
    title: str
    url: str
    updated_at: str
    is_draft: bool
    approved: bool
    more_approval_required: bool
    approved_by: list[str]
    changes_requested_by: list[str]
    pending_reviewers: list[str]
    human_reviewers: list[str]
    bot_reviewers: list[str]
    review_comments_awaiting_author: dict[str, int]
    threads_awaiting_author: dict[str, int]
    threads_awaiting_reviewer: int
    outdated_unresolved: int
    pushed_since_changes_requested: bool
    ci: str
    mergeable: str
    group: Group = "waiting"
    notes: list[str] = field(default_factory=list)


def gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


def search_ids(scope: str, author: str) -> list[str]:
    target = ["--repo", scope] if "/" in scope else ["--owner", scope]
    out = gh(
        "search", "prs", *target, "--author", author, "--state", "open",
        "--json", "id", "--limit", str(SEARCH_LIMIT),
    )  # fmt: skip
    return [pr["id"] for pr in json.loads(out)]


def fetch_details(ids: list[str]) -> list[dict]:
    out = gh(
        "api",
        "graphql",
        "-f",
        f"query={DETAIL_QUERY}",
        *[arg for i in ids for arg in ("-f", f"ids[]={i}")],
    )
    return json.loads(out)["data"]["nodes"]


def login(actor: dict | None) -> str:
    return actor["login"] if actor else "ghost"


def build_status(node: dict) -> PRStatus:
    author = login(node["author"])
    opinions = node["latestOpinionatedReviews"]["nodes"]
    approved_by = [login(r["author"]) for r in opinions if r["state"] == "APPROVED"]
    changes_by = [
        login(r["author"]) for r in opinions if r["state"] == "CHANGES_REQUESTED"
    ]
    decision = node["reviewDecision"]
    approved = decision == "APPROVED" or (
        decision is None and bool(approved_by) and not changes_by
    )

    awaiting_author: dict[str, int] = {}
    awaiting_reviewer = outdated = 0
    for thread in node["reviewThreads"]["nodes"]:
        if thread["isResolved"]:
            continue
        outdated += thread["isOutdated"]
        if login(thread["last"]["nodes"][0]["author"]) == author:
            awaiting_reviewer += 1
        else:
            opener = login(thread["first"]["nodes"][0]["author"])
            awaiting_author[opener] = awaiting_author.get(opener, 0) + 1

    commit = node["commits"]["nodes"][0]["commit"]
    reviews = node["reviews"]["nodes"]
    others = [r for r in reviews if login(r["author"]) != author]
    is_bot = {
        login(r["author"]): r["author"] and r["author"]["__typename"] == "Bot"
        for r in others
    }
    # The author's latest push, comment, or thread reply (thread replies are reviews
    # authored by the PR author) answers any review-body comment made before it.
    author_activity = max(
        [commit["committedDate"]]
        + [r["submittedAt"] for r in reviews if login(r["author"]) == author]
        + [
            c["createdAt"]
            for c in node["comments"]["nodes"]
            if login(c["author"]) == author
        ]
    )
    review_comments: dict[str, int] = {}
    for r in others:
        name = login(r["author"])
        if (
            r["state"] == "COMMENTED"
            and r["bodyText"].strip()
            and not is_bot[name]
            and r["submittedAt"] > author_activity
        ):
            review_comments[name] = review_comments.get(name, 0) + 1
    changes_at = [
        r["submittedAt"] for r in opinions if r["state"] == "CHANGES_REQUESTED"
    ]
    rollup = commit["statusCheckRollup"]
    requested = [
        r["requestedReviewer"]
        for r in node["reviewRequests"]["nodes"]
        if r["requestedReviewer"]
    ]
    status = PRStatus(
        repo=node["repository"]["nameWithOwner"],
        number=node["number"],
        title=node["title"],
        url=node["url"],
        updated_at=node["updatedAt"],
        is_draft=node["isDraft"],
        approved=approved,
        more_approval_required=decision == "REVIEW_REQUIRED"
        and bool(approved_by)
        and not changes_by,
        approved_by=approved_by,
        changes_requested_by=changes_by,
        pending_reviewers=[r.get("login") or r["name"] for r in requested],
        human_reviewers=sorted(name for name, bot in is_bot.items() if not bot),
        bot_reviewers=sorted(name for name, bot in is_bot.items() if bot),
        review_comments_awaiting_author=review_comments,
        threads_awaiting_author=awaiting_author,
        threads_awaiting_reviewer=awaiting_reviewer,
        outdated_unresolved=outdated,
        pushed_since_changes_requested=bool(changes_at)
        and commit["committedDate"] > max(changes_at),
        ci=rollup["state"] if rollup else "NONE",
        mergeable=node["mergeable"],
    )
    if node["reviewThreads"]["totalCount"] > len(node["reviewThreads"]["nodes"]):
        status.notes.append(
            f"only first {len(node['reviewThreads']['nodes'])} threads counted"
        )
    status.group = classify(status)
    return status


def classify(pr: PRStatus) -> Group:
    if pr.is_draft:
        return "draft"
    # A push or a re-request after "changes requested" hands the move back to the reviewer.
    rerequested = set(pr.changes_requested_by) <= set(pr.pending_reviewers)
    blocked_on_author = (
        pr.threads_awaiting_author
        or pr.review_comments_awaiting_author
        or (
            pr.changes_requested_by
            and not (pr.pushed_since_changes_requested or rerequested)
        )
        or pr.ci in ("FAILURE", "ERROR")
        or pr.mergeable == "CONFLICTING"
    )
    if blocked_on_author:
        return "yours"
    if pr.approved and pr.ci in ("SUCCESS", "NONE") and pr.mergeable == "MERGEABLE":
        return "ready"
    if pr.approved or pr.pending_reviewers or pr.human_reviewers:
        return "waiting"
    return "unreviewed"


def describe(pr: PRStatus) -> str:
    parts: list[str] = []
    if pr.approved:
        parts.append(f"approved by {', '.join(pr.approved_by) or 'required reviewers'}")
    elif pr.more_approval_required:
        parts.append(f"approved by {', '.join(pr.approved_by)}, more approval required")
    elif pr.approved_by:
        parts.append(f"approved by {', '.join(pr.approved_by)}")
    if pr.changes_requested_by:
        rerequested = set(pr.changes_requested_by) <= set(pr.pending_reviewers)
        suffix = (
            " (pushed since, not re-requested)"
            if pr.pushed_since_changes_requested and not rerequested
            else ""
        )
        parts.append(
            f"changes requested by {', '.join(pr.changes_requested_by)}{suffix}"
        )
    if pr.threads_awaiting_author:
        total = sum(pr.threads_awaiting_author.values())
        who = ", ".join(f"{name} {n}" for name, n in pr.threads_awaiting_author.items())
        parts.append(f"{plural(total, 'open thread')} awaiting the author ({who})")
    if pr.review_comments_awaiting_author:
        who = ", ".join(pr.review_comments_awaiting_author)
        parts.append(
            f"review comment from {who} newer than the author's last push or reply"
        )
    if pr.threads_awaiting_reviewer:
        parts.append(
            f"{plural(pr.threads_awaiting_reviewer, 'open thread')} the author replied to last"
        )
    if pr.outdated_unresolved:
        parts.append(f"{pr.outdated_unresolved} open outdated by later pushes")
    if pr.pending_reviewers:
        parts.append(f"review requested from {', '.join(pr.pending_reviewers)}")
    if pr.human_reviewers and not (pr.approved_by or pr.changes_requested_by):
        parts.append(f"reviewed by {', '.join(pr.human_reviewers)}")
    elif pr.bot_reviewers and not pr.human_reviewers:
        parts.append(f"bot review only ({', '.join(pr.bot_reviewers)})")
    ci = {
        "FAILURE": "CI failing",
        "ERROR": "CI erroring",
        "PENDING": "CI running",
        "EXPECTED": "CI not started",
    }
    if pr.ci in ci:
        parts.append(ci[pr.ci])
    if pr.mergeable == "CONFLICTING":
        parts.append("merge conflict")
    elif pr.mergeable == "UNKNOWN":
        parts.append("mergeability not computed yet")
    return "; ".join(parts + pr.notes) or "no reviews or requests"


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def age(updated_at: str, now: datetime) -> str:
    days = (now - datetime.fromisoformat(updated_at)).days
    return "today" if days == 0 else f"{days}d"


def render(prs: list[PRStatus]) -> str:
    now = datetime.now(UTC)
    lines: list[str] = []
    for group, title in GROUP_TITLES.items():
        members = sorted(
            (p for p in prs if p.group == group), key=lambda p: p.updated_at
        )
        if not members:
            continue
        lines += [
            f"## {title} ({len(members)})",
            "",
            "| PR | Title | Status | Updated |",
            "|---|---|---|---|",
        ]
        for p in members:
            title_cell = p.title.replace("|", "\\|")
            lines.append(
                f"| [{p.repo}#{p.number}]({p.url}) | {title_cell} | {describe(p)} | {age(p.updated_at, now)} |"
            )
        lines.append("")
    counts = {g: sum(p.group == g for p in prs) for g in GROUP_TITLES}
    lines.append(
        f"{len(prs)} open: {counts['ready']} ready to merge, {counts['yours']} need you, "
        f"{counts['waiting']} waiting on others, {counts['unreviewed']} with no human reviewer, {counts['draft']} drafts."
    )
    return "\n".join(lines)


@app.default
def main(
    scope: str,
    *,
    author: str = "@me",
    as_json: Annotated[bool, Parameter(name="--json")] = False,
) -> None:
    """Report open PRs grouped by whose move it is.

    :param scope: An org or user (e.g. ``mitodl``) or one repo (``mitodl/agent-kit``).
    :param author: PR author to report on.
    :param as_json: Emit one JSON object per PR instead of the markdown report.
    """
    ids = search_ids(scope, author)
    batches = [ids[i : i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]
    with ThreadPoolExecutor(MAX_WORKERS) as pool:
        nodes = [node for batch in pool.map(fetch_details, batches) for node in batch]
    prs = [build_status(node) for node in nodes]
    if as_json:
        json.dump([asdict(p) for p in prs], sys.stdout, indent=2)
    else:
        print(render(prs))


if __name__ == "__main__":
    app()
