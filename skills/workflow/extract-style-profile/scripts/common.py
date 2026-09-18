"""Shared helpers for the extract-style-profile scripts (stdlib only)."""

import json
import re
import subprocess
import sys
import time
from datetime import date
from itertools import pairwise
from pathlib import Path

BOT_AUTHOR = re.compile(
    r"\[bot\]|^(renovate|dependabot|pre-commit-ci|github-actions|copilot-swe-agent|snyk-bot)\b",
    re.IGNORECASE,
)
AI_COMMIT_MARKERS = re.compile(
    r"co-authored-by:.*(claude|copilot|aider|anthropic|openai|gemini|cursor|codex|devin)"
    r"|generated with \[?(claude|copilot|aider|cursor)"
    r"|^apply suggestions? from (code review|@copilot|@gemini)",
    re.IGNORECASE | re.MULTILINE,
)
WEB_SUGGESTION_SUBJECT = re.compile(r"^Update [\w./-]+\.\w+$")


def load_subject(workdir: Path) -> dict:
    subject = json.loads((workdir / "subject.json").read_text())
    subject.setdefault("members", [])
    subject.setdefault("orgs", [])
    subject.setdefault("repos", [])
    subject.setdefault("exclude_repo_patterns", ["-ghsa-"])
    subject.setdefault("local_roots", [])
    subject.setdefault("min_commits", 3)
    subject.setdefault("full_clone_min_commits", 20)
    if subject["mode"] not in {"person", "team", "repo"}:
        raise ValueError(f"mode must be person, team or repo, not {subject['mode']!r}")
    if subject["mode"] == "repo" and not subject["repos"]:
        raise ValueError("repo mode needs at least one owner/name in repos")
    if subject["mode"] != "repo" and not subject["members"]:
        raise ValueError("person and team modes need at least one entry in members")
    for member in subject["members"]:
        # An empty pattern matches every author and would pull coworkers' commits
        # into a personal profile.
        if not [p for p in member.get("author_patterns", []) if p.strip()]:
            raise ValueError(
                f"member {member.get('login')!r} needs at least one non-empty author_pattern"
            )
    _validate_eras(subject)
    return subject


def _validate_eras(subject: dict) -> None:
    """Eras must tile since..until exactly, with ai_cutoff on a boundary.

    A gap makes era_of return None, and build_corpora drops those records silently.
    """
    for key in ("since", "until", "ai_cutoff"):
        date.fromisoformat(subject[key])
    eras = sorted(subject.get("eras") or [], key=lambda era: era["start"])
    if not eras:
        raise ValueError(
            "define eras in subject.json (see references/subject-schema.md)"
        )
    if eras[0]["start"] != subject["since"] or eras[-1]["end"] != subject["until"]:
        raise ValueError(
            f"eras must start at since ({subject['since']}) and end at until ({subject['until']}), "
            f"got {eras[0]['start']}..{eras[-1]['end']}"
        )
    for previous, current in pairwise(eras):
        if previous["end"] != current["start"]:
            raise ValueError(
                f"eras {previous['name']} and {current['name']} leave a gap or overlap "
                f"({previous['end']} vs {current['start']})"
            )
    for era in eras:
        if not era["start"] < era["end"]:
            raise ValueError(f"era {era['name']} is empty or reversed")
    boundaries = {era["start"] for era in eras} | {subject["until"]}
    if subject["ai_cutoff"] not in boundaries:
        raise ValueError(
            f"ai_cutoff {subject['ai_cutoff']} must fall on an era boundary: {sorted(boundaries)}"
        )
    subject["eras"] = eras


def era_of(subject: dict, iso_date: str) -> str | None:
    day = iso_date[:10]
    for era in subject["eras"]:
        if era["start"] <= day < era["end"]:
            return era["name"]
    return None


def is_baseline(subject: dict, iso_date: str) -> bool:
    return iso_date[:10] < subject["ai_cutoff"]


def login_for_author(subject: dict, name: str, email: str) -> str | None:
    for member in subject["members"]:
        for pattern in member["author_patterns"]:
            if re.search(pattern, f"{name} <{email}>", re.IGNORECASE):
                return member["login"]
    return None


def repo_dirname(repo: str) -> str:
    return repo.replace("/", "__")


def graphql(query: str, *, required: bool = True, **variables) -> dict | None:
    """Run a GraphQL query through gh.

    Rate-limit errors back off for minutes, because GitHub's secondary limits need
    it. Other errors (including GitHub's "Something went wrong", which some nodes
    trigger deterministically) retry briefly, then raise. A silent empty result
    once truncated years of PRs without any error, so failures only pass quietly
    when the caller opts in with required=False.
    """
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        flag = "-F" if isinstance(value, int) else "-f"
        args += [flag, f"{key}={value}"]
    attempts = 0
    while True:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return json.loads(result.stdout)["data"]
        attempts += 1
        rate_limited = re.search(
            r"rate limit|abuse|secondary", result.stderr, re.IGNORECASE
        )
        if attempts >= (6 if rate_limited else 3):
            if required:
                raise RuntimeError(
                    f"graphql query failed: {result.stderr.strip()[:500]}"
                )
            return None
        wait = 60 * attempts if rate_limited else 5 * attempts
        print(
            f"graphql error (attempt {attempts}), retrying in {wait}s: {result.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        time.sleep(wait)


def write_jsonl(path: Path, rows) -> int:
    """Write rows atomically so a concurrent reader never sees a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with tmp.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
            count += 1
    tmp.replace(path)
    return count


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
