# /// script
# requires-python = ">=3.11"
# dependencies = ["cyclopts>=3"]
# ///
"""Fetch PRs, issues, comments, reviews and discussions into data/*.jsonl.

Each kind writes its own files atomically, so running different kinds in separate
processes is safe. Running the *same* kind twice at once is not: two writers on
one file corrupted the PR dump in the original run. In repo mode, `prs`,
`reviews` and `comments` all run the PR query, so they count as the same kind.
"""

import re
import sys
from datetime import UTC, date, datetime, timedelta
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
    ... on PullRequest{id url title body createdAt additions deletions changedFiles merged
      author{login} repository{nameWithOwner} commits{totalCount} %(pr_extra)s}
    ... on Issue{id url title body createdAt author{login} repository{nameWithOwner} %(issue_extra)s}}}}
"""
PAGE_INFO = "pageInfo{hasNextPage endCursor}"
INLINE_COMMENT_FIELDS = "body path diffHunk"
COMMENT_FIELDS = "author{login} body createdAt url"
REVIEW_FIELDS = (
    "id author{login} body state createdAt url "
    f"comments(first:30){{{PAGE_INFO} nodes{{{INLINE_COMMENT_FIELDS}}}}}"
)
REPO_PR_EXTRA = (
    f"reviews(first:30){{{PAGE_INFO} nodes{{{REVIEW_FIELDS}}}}} "
    f"comments(first:50){{{PAGE_INFO} nodes{{{COMMENT_FIELDS}}}}}"
)
REPO_ISSUE_EXTRA = f"comments(first:50){{{PAGE_INFO} nodes{{{COMMENT_FIELDS}}}}}"
# Continues a nested connection past its first page, from the parent's node id.
NODE_CONNECTION = """
query($id:ID!,$c:String){node(id:$id){... on %(type)s{
  %(field)s(first:100,after:$c){pageInfo{hasNextPage endCursor} nodes{%(fields)s}}}}}
"""

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
    nodes{pullRequestReview{id url body state createdAt repository{nameWithOwner}
      pullRequest{title author{login}}
      comments(first:50){pageInfo{hasNextPage endCursor} nodes{body path diffHunk}}}}}}}}
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
  nodes{id url title body createdAt author{login} category{name}
    comments(first:50){pageInfo{hasNextPage endCursor} nodes{url body createdAt author{login}}}}}}}
"""


def _login(node: dict | None) -> str | None:
    return (node or {}).get("login")


def _paginate(query: str, path: list[str], stop=None, after=None, **variables):
    """Yield nodes page by page. `stop(node)` ends pagination early on date-ordered connections."""
    cursor, pages, label = after, 0, path[-1]
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


def _all_nodes(parent: dict, type_name: str, field: str, fields: str) -> list[dict]:
    """Every node of a nested connection, fetching pages beyond the first one inline.

    Nested connections are capped (first:30/50) to keep the parent query cheap;
    without this, a busy PR's later reviews and comments silently vanish.
    """
    connection = parent[field]
    nodes = list(connection["nodes"])
    if connection["pageInfo"]["hasNextPage"]:
        nodes += _paginate(
            NODE_CONNECTION % {"type": type_name, "field": field, "fields": fields},
            ["node", field],
            after=connection["pageInfo"]["endCursor"],
            id=parent["id"],
        )
    return nodes


def _review_row(
    review: dict,
    repo: str,
    pr_title: str,
    pr_author: str | None,
    login: str | None = None,
) -> dict:
    return {
        "login": login or _login(review.get("author")),
        "repo": repo,
        "url": review["url"],
        "createdAt": review["createdAt"],
        "state": review["state"],
        "body": review["body"],
        "pr_title": pr_title,
        "pr_author": pr_author,
        "comments": _all_nodes(
            review, "PullRequestReview", "comments", INLINE_COMMENT_FIELDS
        ),
    }


def _search(
    qualifiers: str,
    since: str,
    until: str,
    extras: dict,
    page: int = 100,
    field: str = "created",
):
    """Search one date range on `field`, bisecting until each slice is under GitHub's 1,000 result cap."""
    query = SEARCH % extras
    start, end = (
        date.fromisoformat(since),
        date.fromisoformat(until) - timedelta(days=1),
    )
    if start > end:
        return
    probe = graphql(query, q=f"{qualifiers} {field}:{start}..{end}", n=1)
    if probe["search"]["issueCount"] > 1000 and start < end:
        mid = start + (end - start) / 2
        yield from _search(
            qualifiers, str(start), str(mid + timedelta(days=1)), extras, page, field
        )
        yield from _search(
            qualifiers, str(mid + timedelta(days=1)), until, extras, page, field
        )
        return
    yield from _paginate(
        query, ["search"], q=f"{qualifiers} {field}:{start}..{end}", n=page
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
                            _review_row(
                                r,
                                r["repository"]["nameWithOwner"],
                                r["pullRequest"]["title"],
                                _login(r["pullRequest"]["author"]),
                                login=login,
                            )
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


def _comment_row(c: dict, repo: str, kind: str, title: str, author: str | None) -> dict:
    return {
        "login": _login(c["author"]),
        "repo": repo,
        "url": c["url"],
        "createdAt": c["createdAt"],
        "body": c["body"],
        "parent_kind": kind,
        "parent_title": title,
        "parent_author": author,
    }


def _fetch_repo(subject: dict, kind: str) -> dict[str, list[dict]]:
    """Repo mode: everyone's activity in the listed repos, with reviews/comments nested under their parent.

    Parents are searched by `updated` from `since` onward, not by `created` in the
    interval: a review or comment written in the interval on an older PR or issue
    bumps its parent's updatedAt, so this finds it. `_in_scope` then keeps each
    row by its own createdAt. PR and issue comments go to separate files so the
    `prs` and `issues` kinds can run in parallel without overwriting each other.
    """
    out: dict[str, list[dict]] = {}
    search_until = str(datetime.now(UTC).date() + timedelta(days=2))
    for repo in subject["repos"]:
        if kind == "prs":
            for n in _search(
                f"repo:{repo} is:pr",
                subject["since"],
                search_until,
                {"pr_extra": REPO_PR_EXTRA, "issue_extra": ""},
                page=25,
                field="updated",
            ):
                if n["__typename"] != "PullRequest":
                    continue
                pr_author = _login(n["author"])
                out.setdefault("prs", []).append(_pr_row(n))
                for r in _all_nodes(n, "PullRequest", "reviews", REVIEW_FIELDS):
                    out.setdefault("reviews", []).append(
                        _review_row(r, repo, n["title"], pr_author)
                    )
                for c in _all_nodes(n, "PullRequest", "comments", COMMENT_FIELDS):
                    out.setdefault("comments_pr", []).append(
                        _comment_row(c, repo, "pr", n["title"], pr_author)
                    )
        elif kind == "issues":
            for n in _search(
                f"repo:{repo} is:issue",
                subject["since"],
                search_until,
                {"pr_extra": "", "issue_extra": REPO_ISSUE_EXTRA},
                page=50,
                field="updated",
            ):
                if n["__typename"] != "Issue":
                    continue
                out.setdefault("issues", []).append(_issue_row(n))
                for c in _all_nodes(n, "Issue", "comments", COMMENT_FIELDS):
                    out.setdefault("comments_issue", []).append(
                        _comment_row(c, repo, "issue", n["title"], _login(n["author"]))
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
                for c in _all_nodes(
                    n, "Discussion", "comments", "url body createdAt author{login}"
                ):
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

    In repo mode, `prs` also yields reviews and PR comments (`comments_pr.jsonl`),
    and `issues` also yields issue comments (`comments_issue.jsonl`), because they
    are nested in the same query. Run `prs`, `reviews` and `comments` in one
    process there; they all resolve to the PR query.
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
