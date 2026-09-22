# /// script
# requires-python = ">=3.11"
# dependencies = ["cyclopts>=3"]
# ///
"""Quantitative baselines: yearly commit-message metrics and a monthly AI-marker timeline."""

import collections
import re
import statistics
from pathlib import Path

import cyclopts
from common import load_subject, read_jsonl

app = cyclopts.App()

CONVENTIONAL = re.compile(r"^[a-z][a-z,]*(\([^)]*\))?!?:\s")
PROSE_AI_MARKERS = {
    "summary/root-cause headers": re.compile(
        r"^#{1,3} (Summary|Root Cause|Key Changes|Problem|Solution|Changes Made)\b",
        re.MULTILINE,
    ),
    "check/warn emoji": re.compile("[✅⚠❌\U0001f680]"),
    "bold-label bullets": re.compile(r"^\s*[-*] \*\*[^*]+\*\*:?", re.MULTILINE),
    "tool attribution": re.compile(
        r"generated with|co-authored-by: (claude|copilot)|via aider", re.IGNORECASE
    ),
    "ai review commands": re.compile(
        r"^/(gemini|copilot|claude)\b|@(claude|copilot)\b", re.MULTILINE
    ),
    "inflated vocabulary": re.compile(
        r"\b(comprehensive|seamless(ly)?|robust|leverag(e|ing))\b", re.IGNORECASE
    ),
}


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}" if whole else "-"


SCOPED = re.compile(r"^[a-z,]+\(")
PAST = re.compile(r"^\w+ed\b")
GERUND = re.compile(r"^\w+ing\b")
TYPE_PREFIX = re.compile(r"^[a-z][a-z,]*(\([^)]*\))?!?:\s*")
TRAILER = re.compile(r"(co-authored-by|signed-off-by):", re.IGNORECASE)


def _has_body(msg: str) -> bool:
    return any(line.strip() and not TRAILER.match(line) for line in msg.split("\n")[1:])


def _commit_table(rows: list[dict], key) -> list[str]:
    lines = [
        (
            "| group | n | subject len (median) | body % | conventional % | scoped % | ends with . % "
            "| capitalized % | past-tense % | gerund % | median lines | median files | AI-flag % |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    groups = collections.defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    for group in sorted(groups):
        rs = groups[group]
        n = len(rs)
        subjects = [r["msg"].split("\n", 1)[0] for r in rs]
        stripped = [TYPE_PREFIX.sub("", s) for s in subjects]
        stat_rows = [r for r in rs if r["has_stat"]]
        cells = [
            group,
            n,
            f"{statistics.median(len(s) for s in subjects):.0f}",
            _pct(sum(_has_body(r["msg"]) for r in rs), n),
            _pct(sum(bool(CONVENTIONAL.match(s)) for s in subjects), n),
            _pct(sum(bool(SCOPED.match(s)) for s in subjects), n),
            _pct(sum(s.rstrip().endswith(".") for s in subjects), n),
            _pct(sum(s[:1].isupper() for s in stripped), n),
            _pct(sum(bool(PAST.match(s)) for s in stripped), n),
            _pct(sum(bool(GERUND.match(s)) for s in stripped), n),
            statistics.median(r["insertions"] + r["deletions"] for r in stat_rows)
            if stat_rows
            else "-",
            statistics.median(r["files"] for r in stat_rows) if stat_rows else "-",
            _pct(sum(r["ai_flag"] for r in rs), n),
        ]
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    return lines


@app.default
def stats(workdir: Path) -> None:
    """Write stats/commit_stats.md and stats/ai_markers.md.

    Read ai_markers.md before fixing ai_cutoff in subject.json: the cutoff is the
    first month where markers become sustained, not the first isolated hit.
    """
    subject = load_subject(workdir)
    data = workdir / "data"
    out = workdir / "stats"
    out.mkdir(exist_ok=True)
    commits = [
        r
        for r in read_jsonl(data / "commits.jsonl")
        if not (r["merge"] or r["bot"] or r["web_suggestion"])
    ]

    report = ["# Commit message metrics", "", "## By year", ""]
    report += _commit_table(commits, lambda r: r["date"][:4])
    report += ["", "## By era", ""] + _commit_table(
        commits, lambda r: r["era"] or "out-of-range"
    )
    if subject["mode"] != "repo":
        report += ["", "## By member (baseline only)", ""]
        report += _commit_table(
            [r for r in commits if r["baseline"]],
            lambda r: r["login"] or "unattributed",
        )
    else:
        top = collections.Counter(
            r["login"] or r["author"] for r in commits
        ).most_common(15)
        report += ["", "## Top authors", ""] + [
            f"- {name}: {count}" for name, count in top
        ]
    for era in [e["name"] for e in subject["eras"]]:
        rs = [r for r in commits if r["era"] == era]
        if not rs:
            continue
        first = collections.Counter(
            TYPE_PREFIX.sub("", r["msg"]).split(" ", 1)[0] for r in rs
        )
        prefixes = collections.Counter(
            m.group(1)
            for r in rs
            if (m := re.match(r"^([a-z][a-z,]*)(\([^)]*\))?!?:\s", r["msg"]))
        )
        report += [
            "",
            f"## {era}",
            "",
            f"First words: {first.most_common(15)}",
            "",
            f"Type prefixes: {prefixes.most_common(15)}",
        ]
    (out / "commit_stats.md").write_text("\n".join(report) + "\n")

    months: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    for row in read_jsonl(data / "commits.jsonl"):
        months[row["date"][:7]]["items"] += 1
        if row["ai_flag"]:
            months[row["date"][:7]]["commit trailers/authors"] += 1
        if re.match(r"^[a-z]+\([^)]+\): [a-z]", row["msg"]):
            months[row["date"][:7]]["scoped lowercase commits"] += 1
    for path in sorted(data.glob("*.jsonl")):
        name = path.stem
        if name == "commits":
            continue
        for row in read_jsonl(path):
            months[row["createdAt"][:7]]["items"] += 1
            inline = [c.get("body") or "" for c in row.get("comments") or []]
            text = "\n".join([row.get("title") or "", row.get("body") or "", *inline])
            for label, pattern in PROSE_AI_MARKERS.items():
                if pattern.search(text):
                    months[row["createdAt"][:7]][f"{name}: {label}"] += 1
    labels = ["items"] + sorted(
        {label for counter in months.values() for label in counter} - {"items"}
    )
    timeline = [
        "# AI-marker timeline",
        "",
        (
            "Counts per month, with total activity in the `items` column. Look for the month where hits "
            "become sustained relative to activity; isolated early hits are usually false positives "
            "(e.g. an emoji that came from an upstream template)."
        ),
        "",
    ]
    timeline.append("| month | " + " | ".join(labels) + " |")
    timeline.append("|---|" + "---|" * len(labels))
    for month in sorted(months):
        timeline.append(
            f"| {month} | "
            + " | ".join(str(months[month][label] or "") for label in labels)
            + " |"
        )
    (out / "ai_markers.md").write_text("\n".join(timeline) + "\n")
    print(
        f"wrote {out / 'commit_stats.md'} and {out / 'ai_markers.md'} ({len(commits)} human commits)"
    )


if __name__ == "__main__":
    app()
