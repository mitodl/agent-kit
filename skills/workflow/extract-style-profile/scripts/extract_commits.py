# /// script
# requires-python = ">=3.11"
# dependencies = ["cyclopts>=3"]
# ///
"""Extract commits in scope into data/commits.jsonl, deduplicated by SHA and flagged."""

import json
import re
import subprocess
from pathlib import Path

import cyclopts
from common import (
    AI_COMMIT_MARKERS,
    BOT_AUTHOR,
    WEB_SUGGESTION_SUBJECT,
    era_of,
    is_baseline,
    load_subject,
    login_for_author,
    write_jsonl,
)

app = cyclopts.App()
RECORD, FIELD = "\x1e", "\x1f"


def _stat(line: str) -> dict:
    def get(pattern: str) -> int:
        match = re.search(pattern, line)
        return int(match.group(1)) if match else 0

    return {
        "files": get(r"(\d+) files? changed"),
        "insertions": get(r"(\d+) insertion"),
        "deletions": get(r"(\d+) deletion"),
    }


@app.default
def extract(workdir: Path) -> None:
    """Walk every repo in repos/manifest.json.

    Mirrors and forks share SHAs, so the first repo in manifest order (most
    commits) owns each SHA. Shortstat is only collected for full clones and
    local checkouts; on blobless clones it would fetch every blob lazily.
    """
    subject = load_subject(workdir)
    manifest = json.loads((workdir / "repos" / "manifest.json").read_text())
    author_args = []
    if subject["mode"] != "repo":
        patterns = [p for m in subject["members"] for p in m["author_patterns"]]
        author_args = [
            "-i",
            "--perl-regexp",
            "--author=" + "|".join(f"(?:{p})" for p in patterns),
        ]
    seen: dict[str, dict] = {}
    for entry in manifest:
        if "error" in entry:
            continue
        fmt = f"{RECORD}%H{FIELD}%aI{FIELD}%an{FIELD}%ae{FIELD}%cn{FIELD}%P{FIELD}%B{FIELD}"
        cmd = [
            "git",
            "-C",
            entry["path"],
            "log",
            "--all",
            f"--since={subject['since']}",
            f"--until={subject['until']}",
            *author_args,
            f"--format={fmt}",
        ]
        if not entry["partial"]:
            cmd.append("--shortstat")
        out = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", check=False
        ).stdout
        for record in out.split(RECORD)[1:]:
            sha, date, name, email, committer, parents, body, tail = record.split(FIELD)
            if sha in seen:
                continue
            msg = body.strip()
            subject_line = msg.split("\n", 1)[0]
            login = login_for_author(subject, name, email)
            seen[sha] = {
                "repo": entry["repo"],
                "sha": sha,
                "date": date,
                "author": name,
                "email": email,
                "login": login,
                "committer": committer,
                "merge": len(parents.split()) > 1,
                "msg": msg,
                **_stat(tail),
                "has_stat": not entry["partial"],
                "era": era_of(subject, date),
                "baseline": is_baseline(subject, date),
                "bot": bool(BOT_AUTHOR.search(name)),
                "ai_flag": bool(
                    AI_COMMIT_MARKERS.search(msg)
                    or re.search(r"\(aider\)", name, re.IGNORECASE)
                ),
                "web_suggestion": bool(WEB_SUGGESTION_SUBJECT.match(subject_line)),
            }
    rows = sorted(seen.values(), key=lambda r: r["date"])
    (workdir / "data").mkdir(exist_ok=True)
    count = write_jsonl(workdir / "data" / "commits.jsonl", rows)
    human = [r for r in rows if not (r["merge"] or r["bot"])]
    print(
        f"{count} commits ({len(human)} non-merge human, {sum(r['ai_flag'] for r in rows)} AI-flagged, "
        f"{sum(r['web_suggestion'] for r in rows)} web-UI suggestion commits)"
    )


if __name__ == "__main__":
    app()
