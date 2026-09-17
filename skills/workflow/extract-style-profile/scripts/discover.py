# /// script
# requires-python = ">=3.11"
# dependencies = ["cyclopts>=3"]
# ///
"""Find the repositories and git identities behind each member's GitHub activity."""

import collections
import json
import re
import subprocess
from datetime import date, timedelta
from pathlib import Path

import cyclopts
from common import graphql, load_subject

app = cyclopts.App()

CONTRIBUTIONS = """
query($login:String!,$from:DateTime!,$to:DateTime!){user(login:$login){contributionsCollection(from:$from,to:$to){
  totalCommitContributions totalPullRequestContributions totalIssueContributions
  totalPullRequestReviewContributions restrictedContributionsCount
  commitContributionsByRepository(maxRepositories:100){
    repository{nameWithOwner isFork primaryLanguage{name}} contributions{totalCount}}}}}
"""


def _identities(login: str) -> collections.Counter:
    """Sample commit author name/email pairs GitHub has linked to the login."""
    result = subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "GET",
            "search/commits",
            "-f",
            f"q=author:{login}",
            "-f",
            "per_page=100",
            "--jq",
            '.items[].commit.author | "\\(.name) <\\(.email)>"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return collections.Counter(line for line in result.stdout.splitlines() if line)


@app.default
def discover(workdir: Path) -> None:
    """Write activity/yearly.md, activity/repos.tsv and activity/identities.md.

    Person/team mode only. Repo mode takes its repo list straight from subject.json.
    """
    subject = load_subject(workdir)
    if subject["mode"] == "repo":
        print("repo mode: nothing to discover, repos come from subject.json")
        return
    out = workdir / "activity"
    out.mkdir(exist_ok=True)
    start_year, end_year = (
        int(subject["since"][:4]),
        (date.fromisoformat(subject["until"]) - timedelta(days=1)).year,
    )
    repo_commits: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    languages: dict[str, str] = {}
    yearly_lines = [
        "| login | year | commits | PRs | issues | reviews | private (hidden) |",
        "|---|---|---|---|---|---|---|",
    ]
    identity_lines = []
    for member in subject["members"]:
        login = member["login"]
        for year in range(start_year, end_year + 1):
            # Clamp to since/until so a partial first or last year doesn't pull
            # out-of-range repos into the clone set.
            window_start = max(f"{year}-01-01", subject["since"])
            window_end = min(f"{year + 1}-01-01", subject["until"])
            data = graphql(
                CONTRIBUTIONS,
                login=login,
                **{
                    "from": f"{window_start}T00:00:00Z",
                    "to": f"{window_end}T00:00:00Z",
                },
            )
            coll = data["user"]["contributionsCollection"]
            (out / f"contrib_{login}_{year}.json").write_text(json.dumps(coll))
            yearly_lines.append(
                f"| {login} | {year} | {coll['totalCommitContributions']} | {coll['totalPullRequestContributions']} "
                f"| {coll['totalIssueContributions']} | {coll['totalPullRequestReviewContributions']} | {coll['restrictedContributionsCount']} |"
            )
            for entry in coll["commitContributionsByRepository"]:
                repo = entry["repository"]["nameWithOwner"]
                repo_commits[repo][login] += entry["contributions"]["totalCount"]
                languages[repo] = (entry["repository"]["primaryLanguage"] or {}).get(
                    "name", "-"
                )
        identity_lines.append(f"## {login}\n")
        identity_lines += [
            f"- {count}x `{ident}` → `^{re.escape(ident)}`"
            for ident, count in _identities(login).most_common(15)
        ]
        identity_lines.append("")

    exclude = [re.compile(p) for p in subject["exclude_repo_patterns"]]
    rows = []
    for repo, per_login in repo_commits.items():
        if subject["orgs"] and repo.split("/")[0] not in subject["orgs"]:
            continue
        if any(p.search(repo) for p in exclude):
            continue
        rows.append((sum(per_login.values()), repo, languages[repo], len(per_login)))
    rows.sort(reverse=True)
    with (out / "repos.tsv").open("w") as handle:
        handle.write("commits\trepo\tlanguage\tmembers\n")
        for row in rows:
            handle.write("\t".join(map(str, row)) + "\n")
    (out / "yearly.md").write_text("\n".join(yearly_lines) + "\n")
    (out / "identities.md").write_text(
        "Candidate git identities per login (from GitHub commit search), each with an anchored regex. "
        "Copy the real ones into members[].author_patterns. Keep patterns anchored: a loose pattern "
        "like `Sam` also matches `Samantha` and misattributes commits.\n\n"
        + "\n".join(identity_lines)
    )
    print("\n".join(yearly_lines))
    print(
        f"\n{len(rows)} repos -> {out / 'repos.tsv'}; identities -> {out / 'identities.md'}"
    )


if __name__ == "__main__":
    app()
