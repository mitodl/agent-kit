# /// script
# requires-python = ">=3.11"
# dependencies = ["cyclopts>=3"]
# ///
"""Fetch PRs, issues, comments, reviews and discussions into data/*.jsonl.

Every kind writes its own file atomically at the end, so running several kinds in
separate processes is safe. Running the *same* kind twice at once is not: two
writers on one file corrupted the PR dump in the original run.
"""

import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

import cyclopts
from common import graphql, load_subject, write_jsonl
from cyclopts import Parameter

app = cyclopts.App()
Kind = Literal["prs", "issues", "comments", "reviews", "discussions"]

SEARCH = """
query($q:String!,$c:String,$n:Int!){search(query:$q,type:ISSUE,first:$n,after:$c){
  issueCount pageInfo{hasNextPage endCursor}
  nodes{__typename
    ... on PullRequest{url title body createdAt additions deletions changedFiles merged
      author{login} repository{nameWithOwner} commits{totalCount} %(pr_extra)s}
    ... on Issue{url title body createdAt author{login} repository{nameWithOwner} %(issue_extra)s}}}}
"""
REPO_PR_EXTRA = """
  reviews(first:30){nodes{author{login} body state createdAt url comments(first:30){nodes{body path diffHunk}}}}
  comments(first:50){nodes{author{login} body createdAt url}}"""
REPO_ISSUE_EXTRA = "comments(first:50){nodes{author{login} body createdAt url}}"

USER_COMMENTS = """
query($login:String!,$c:String){user(login:$login){issueComments(first:100,after:$c,orderBy:{field:UPDATED_AT,direction:DESC}){

  pageInfo{hasNextPage endCursor}
  nodes{url body createdAt updatedAt repository{nameWithOwner}
    issue{title author{login}} pullRequest{title author{login}}}}}}
"""
USER_REVIEWS = """
query($login:String!,$from:DateTime!,$to:DateTime!,$c:String){user(login:$login){
  contributionsCollection(from:$from,to:$to){pullRequestReviewContributions(first:100,after:$c){
    pageInfo{hasNextPage endCursor}
    nodes{pullRequestReview{url body state createdAt repository{nameWithOwner}
      pullRequest{title author{login}} comments(first:50){nodes{body path diffHunk}}}}}}}}
"""
USER_DISCUSSIONS = """
query($login:String!,$c:String){user(login:$login){repositoryDiscussions(first:100,after:$c,orderBy:{field:CREATED_AT,direction:DESC}){

  pageInfo{hasNextPage endCursor}
  nodes{url title body createdAt repository{nameWithOwner} category{name}}}}}
"""
USER_DISCUSSION_COMMENTS = """
query($login:String!,$c:String){user(login:$login){repositoryDiscussionComments(first:40,after:$c){
  pageInfo{hasNextPage endCursor}
  nodes{url body createdAt}}}}
"""
DISCUSSION_TITLE = """
query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){discussion(number:$number){title}}}
"""
DISCUSSION_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/discussions/(\d+)")
REPO_DISCUSSIONS = """
query($owner:String!,$name:String!,$c:String){repository(owner:$owner,name:$name){discussions(first:50,after:$c){
  pageInfo{hasNextPage endCursor}
  nodes{url title body createdAt author{login} category{name}
    comments(first:50){nodes{url body createdAt author{login}}}}}}}
"""


def _login(node: dict | None) -> str | None:
    return (node or {}).get("login")


def _paginate(query: str, path: list[str], stop=None, **variables):
    """Yield nodes page by page. `stop(node)` ends pagination early on date-ordered connections."""
    cursor, pages, label = None, 0, path[-1]
    while True:
        data = graphql(query, c=cursor, **variables)
        for key in path:
            data = data[key]
        pages += 1
        print(f"  {label}: page {pages} ({len(data['nodes'])} nodes)", file=sys.stderr)
        for node in data["nodes"]:
            if stop and stop(node):
                return
            yield node
        if not data["pageInfo"]["hasNextPage"]:
            return
        cursor = data["pageInfo"]["endCursor"]


def _search(qualifiers: str, since: str, until: str, extras: dict, page: int = 100):
    """Search one created-date range, bisecting until each slice is under GitHub's 1,000 result cap."""
    query = SEARCH % extras
    start, end = (
        date.fromisoformat(since),
        date.fromisoformat(until) - timedelta(days=1),
    )
    if start > end:
        return
    probe = graphql(query, q=f"{qualifiers} created:{start}..{end}", n=1)
    if probe["search"]["issueCount"] > 1000 and start < end:
        mid = start + (end - start) / 2
        yield from _search(
            qualifiers, str(start), str(mid + timedelta(days=1)), extras, page
        )
        yield from _search(
            qualifiers, str(mid + timedelta(days=1)), until, extras, page
        )
        return
    yield from _paginate(
        query, ["search"], q=f"{qualifiers} created:{start}..{end}", n=page
    )


def _in_scope(subject: dict, repo: str, created: str) -> bool:
    if not (subject["since"] <= created[:10] < subject["until"]):
        return False
    return not subject["orgs"] or repo.split("/")[0] in subject["orgs"]


def _pr_row(node: dict) -> dict:
    return {
        "login": _login(node["author"]),
        "repo": node["repository"]["nameWithOwner"],
        "url": node["url"],
        "createdAt": node["createdAt"],
        "title": node["title"],
        "body": node["body"] or "",
        "additions": node["additions"],
        "deletions": node["deletions"],
        "changedFiles": node["changedFiles"],
        "commits": node["commits"]["totalCount"],
        "merged": node["merged"],
    }


def _issue_row(node: dict) -> dict:
    return {
        "login": _login(node["author"]),
        "repo": node["repository"]["nameWithOwner"],
        "url": node["url"],
        "createdAt": node["createdAt"],
        "title": node["title"],
        "body": node["body"] or "",
    }


def _fetch_person(subject: dict, kind: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for member in subject["members"]:
        login = member["login"]
        if kind in ("prs", "issues"):
            node_type = "PullRequest" if kind == "prs" else "Issue"
            rows = [
                (_pr_row if kind == "prs" else _issue_row)(n)
                for n in _search(
                    f"author:{login} is:{'pr' if kind == 'prs' else 'issue'}",
                    subject["since"],
                    subject["until"],
                    {"pr_extra": "", "issue_extra": ""},
                )
                if n["__typename"] == node_type
            ]
            out.setdefault(kind, []).extend(rows)
        elif kind == "comments":
            # Ordered by updatedAt desc; once updatedAt < since, createdAt is too.
            for n in _paginate(
                USER_COMMENTS,
                ["user", "issueComments"],
                stop=lambda n: n["updatedAt"][:10] < subject["since"],
                login=login,
            ):
                parent = n["pullRequest"] or n["issue"] or {}
                out.setdefault("comments", []).append(
                    {
                        "login": login,
                        "repo": n["repository"]["nameWithOwner"],
                        "url": n["url"],
                        "createdAt": n["createdAt"],
                        "body": n["body"],
                        "parent_kind": "pr" if n["pullRequest"] else "issue",
                        "parent_title": parent.get("title"),
                        "parent_author": _login(parent.get("author")),
                    }
                )
        elif kind == "reviews":
            year, end_year = (
                int(subject["since"][:4]),
                (date.fromisoformat(subject["until"]) - timedelta(days=1)).year,
            )
            while year <= end_year:
                for quarter in range(4):
                    start = date(year, 1 + quarter * 3, 1)
                    stop = date(
                        year + (quarter == 3), (1 + (quarter + 1) * 3 - 1) % 12 + 1, 1
                    )
                    if str(stop) <= subject["since"] or str(start) >= subject["until"]:
                        continue
                    for n in _paginate(
                        USER_REVIEWS,
                        [
                            "user",
                            "contributionsCollection",
                            "pullRequestReviewContributions",
                        ],
                        login=login,
                        **{"from": f"{start}T00:00:00Z", "to": f"{stop}T00:00:00Z"},
                    ):
                        r = n["pullRequestReview"]
                        out.setdefault("reviews", []).append(
                            {
                                "login": login,
                                "repo": r["repository"]["nameWithOwner"],
                                "url": r["url"],
                                "createdAt": r["createdAt"],
                                "state": r["state"],
                                "body": r["body"],
                                "pr_title": r["pullRequest"]["title"],
                                "pr_author": _login(r["pullRequest"]["author"]),
                                "comments": r["comments"]["nodes"],
                            }
                        )
                year += 1
        elif kind == "discussions":
            for n in _paginate(
                USER_DISCUSSIONS,
                ["user", "repositoryDiscussions"],
                stop=lambda n: n["createdAt"][:10] < subject["since"],
                login=login,
            ):
                out.setdefault("discussions", []).append(
                    {
                        "login": login,
                        "repo": n["repository"]["nameWithOwner"],
                        "url": n["url"],
                        "createdAt": n["createdAt"],
                        "title": n["title"],
                        "body": n["body"],
                        "category": n["category"]["name"],
                    }
                )
            # The comment listing can't ask for comment.discussion: a comment on a
            # deleted discussion makes GitHub fail the whole page. Parse the repo from
            # the URL and look titles up separately, tolerating missing ones.
            titles: dict[str, str | None] = {}
            for n in _paginate(
                USER_DISCUSSION_COMMENTS,
                ["user", "repositoryDiscussionComments"],
                login=login,
            ):
                if not (subject["since"] <= n["createdAt"][:10] < subject["until"]):
                    continue
                owner, name, number = DISCUSSION_URL.search(n["url"]).groups()
                key = f"{owner}/{name}#{number}"
                if key not in titles:
                    found = graphql(
                        DISCUSSION_TITLE,
                        required=False,
                        owner=owner,
                        name=name,
                        number=int(number),
                    )
                    titles[key] = (
                        ((found or {}).get("repository") or {}).get("discussion") or {}
                    ).get("title")
                out.setdefault("discussion_comments", []).append(
                    {
                        "login": login,
                        "repo": f"{owner}/{name}",
                        "url": n["url"],
                        "createdAt": n["createdAt"],
                        "body": n["body"],
                        "parent_title": titles[key],
                    }
                )
    return out


def _fetch_repo(subject: dict, kind: str) -> dict[str, list[dict]]:
    """Repo mode: everyone's activity in the listed repos, with reviews/comments nested under their parent."""
    out: dict[str, list[dict]] = {}
    for repo in subject["repos"]:
        if kind == "prs":
            for n in _search(
                f"repo:{repo} is:pr",
                subject["since"],
                subject["until"],
                {"pr_extra": REPO_PR_EXTRA, "issue_extra": ""},
                page=25,
            ):
                if n["__typename"] != "PullRequest":
                    continue
                out.setdefault("prs", []).append(_pr_row(n))
                for r in n["reviews"]["nodes"]:
                    out.setdefault("reviews", []).append(
                        {
                            "login": _login(r["author"]),
                            "repo": repo,
                            "url": r["url"],
                            "createdAt": r["createdAt"],
                            "state": r["state"],
                            "body": r["body"],
                            "pr_title": n["title"],
                            "pr_author": _login(n["author"]),
                            "comments": r["comments"]["nodes"],
                        }
                    )
                for c in n["comments"]["nodes"]:
                    out.setdefault("comments", []).append(
                        {
                            "login": _login(c["author"]),
                            "repo": repo,
                            "url": c["url"],
                            "createdAt": c["createdAt"],
                            "body": c["body"],
                            "parent_kind": "pr",
                            "parent_title": n["title"],
                            "parent_author": _login(n["author"]),
                        }
                    )
        elif kind == "issues":
            for n in _search(
                f"repo:{repo} is:issue",
                subject["since"],
                subject["until"],
                {"pr_extra": "", "issue_extra": REPO_ISSUE_EXTRA},
                page=50,
            ):
                if n["__typename"] != "Issue":
                    continue
                out.setdefault("issues", []).append(_issue_row(n))
                for c in n["comments"]["nodes"]:
                    out.setdefault("comments", []).append(
                        {
                            "login": _login(c["author"]),
                            "repo": repo,
                            "url": c["url"],
                            "createdAt": c["createdAt"],
                            "body": c["body"],
                            "parent_kind": "issue",
                            "parent_title": n["title"],
                            "parent_author": _login(n["author"]),
                        }
                    )
        elif kind == "discussions":
            owner, name = repo.split("/")
            for n in _paginate(
                REPO_DISCUSSIONS, ["repository", "discussions"], owner=owner, name=name
            ):
                out.setdefault("discussions", []).append(
                    {
                        "login": _login(n["author"]),
                        "repo": repo,
                        "url": n["url"],
                        "createdAt": n["createdAt"],
                        "title": n["title"],
                        "body": n["body"],
                        "category": n["category"]["name"],
                    }
                )
                for c in n["comments"]["nodes"]:
                    out.setdefault("discussion_comments", []).append(
                        {
                            "login": _login(c["author"]),
                            "repo": repo,
                            "url": c["url"],
                            "createdAt": c["createdAt"],
                            "body": c["body"],
                            "parent_title": n["title"],
                        }
                    )
    return out


@app.default
def fetch(
    workdir: Path,
    kinds: Annotated[list[Kind] | None, Parameter(consume_multiple=True)] = None,
) -> None:
    """Fetch the requested kinds for every member (person/team) or repo (repo mode).

    In repo mode, `prs` also yields reviews and PR comments, and `issues` also
    yields issue comments, because they are nested in the same query.
    """
    kinds = kinds or ["prs", "issues", "comments", "reviews", "discussions"]
    subject = load_subject(workdir)
    data_dir = workdir / "data"
    data_dir.mkdir(exist_ok=True)
    fetcher = _fetch_repo if subject["mode"] == "repo" else _fetch_person
    if subject["mode"] == "repo":
        # reviews and PR comments come nested in the PR query; issue comments in the issue query
        kinds = sorted(
            {"prs" if k in ("reviews", "comments") else k for k in kinds}
            | ({"issues"} if "comments" in kinds else set())
        )
    collected: dict[str, list[dict]] = {}
    for kind in kinds:
        print(f"fetching {kind}", file=sys.stderr)
        for name, rows in fetcher(subject, kind).items():
            collected.setdefault(name, []).extend(rows)
        # Write after every kind so a later failure doesn't discard finished work.
        for name, rows in collected.items():
            unique = {
                r["url"]: r
                for r in rows
                if _in_scope(subject, r["repo"], r["createdAt"])
            }
            count = write_jsonl(
                data_dir / f"{name}.jsonl",
                sorted(unique.values(), key=lambda r: r["createdAt"]),
            )
            print(f"{name}: {count}", file=sys.stderr)


if __name__ == "__main__":
    app()
