# /// script
# requires-python = ">=3.11"
# dependencies = ["cyclopts>=3"]
# ///
"""Turn data/*.jsonl and the repos into per-era text corpora the analysis passes read."""

import collections
import json
import random
import re
import subprocess
from pathlib import Path

import cyclopts
from common import BOT_AUTHOR, era_of, load_subject, read_jsonl

app = cyclopts.App()

LANGUAGES = {
    "python": r"\.py$",
    "js_ts": r"\.(tsx?|jsx?|mjs|cjs|vue|svelte)$",
    "go": r"\.go$",
    "rust": r"\.rs$",
    "jvm": r"\.(java|kt|kts|scala|groovy)$",
    "ruby": r"\.rb$",
    "shell": r"\.(sh|bash|zsh)$",
    "container_build": r"(^|/)(Dockerfile|Containerfile|Earthfile|justfile|Makefile)[^/]*$",
    "iac": r"\.(tf|hcl|pkr\.hcl|bicep)$",
    "config_mgmt": r"\.(sls|jinja|j2)$|(^|/)(roles|playbooks)/",
    "yaml_ci": r"\.ya?ml$",
    "sql": r"\.sql$",
    "web_markup": r"\.(html|css|scss|less)$",
    "docs": r"\.(md|rst|org|adoc|txt)$",
}
TEMPLATE_NOISE = re.compile(
    r"(?s:<!--.*?-->)|^#+ .*$|^\s*[-*] \[[ xX]\].*$", re.MULTILINE
)


def _human(login: str | None) -> bool:
    return not (login and BOT_AUTHOR.search(login))


def _own_words(body: str) -> str:
    """PR/issue body minus template scaffolding (HTML comments, headers, checkboxes)."""
    return re.sub(r"\n{3,}", "\n\n", TEMPLATE_NOISE.sub("", body or "")).strip()


def _dominant_language(files: list[str]) -> str | None:
    counts = collections.Counter()
    for name in files:
        for lang, pattern in LANGUAGES.items():
            if re.search(pattern, name):
                counts[lang] += 1
                break
    return counts.most_common(1)[0][0] if counts else None


def _write(path: Path, entries: list[tuple[str, str]]) -> int:
    entries.sort()
    path.write_text("".join(f"\n--- {text}\n" for _, text in entries))
    return len(entries)


@app.default
def build(
    workdir: Path,
    per_language: int = 18,
    per_repo: int = 6,
    per_author: int = 8,
    min_lines: int = 12,
    max_lines: int = 350,
    scan_limit: int = 1200,
    seed: int = 7,
) -> None:
    """Write corpus/{commit_msgs,prs,reviews,conversation,diffs}_<era>.txt and corpus/INDEX.md.

    Diffs are a stratified sample (by dominant language, capped per repo and, in
    team/repo mode, per author) so one busy repo or one prolific teammate can't define "the style".
    """
    subject = load_subject(workdir)
    data, out = workdir / "data", workdir / "corpus"
    out.mkdir(exist_ok=True)
    rng = random.Random(seed)
    buckets: dict[tuple[str, str], list[tuple[str, str]]] = collections.defaultdict(
        list
    )

    def add(kind: str, created: str, text: str) -> None:
        era = era_of(subject, created)
        if era:
            buckets[(kind, era)].append((created, f"{created[:10]} {text}"))

    commits = [
        r
        for r in read_jsonl(data / "commits.jsonl")
        if not (r["merge"] or r["bot"] or r["web_suggestion"])
    ]
    for r in commits:
        who = r["login"] or r["author"]
        flag = " [AI]" if r["ai_flag"] else ""
        stat = (
            f"+{r['insertions']}/-{r['deletions']} files={r['files']}"
            if r["has_stat"]
            else "no-stat"
        )
        add(
            "commit_msgs",
            r["date"],
            f"{r['repo']} {r['sha'][:10]} @{who}{flag} [{stat}]\n{r['msg']}",
        )

    for r in read_jsonl(data / "prs.jsonl"):
        if not _human(r["login"]):
            continue
        own = _own_words(r["body"])
        add(
            "prs",
            r["createdAt"],
            f"{r['repo']} @{r['login']} +{r['additions']}/-{r['deletions']} files={r['changedFiles']} "
            f"commits={r['commits']} merged={r['merged']} own_words_chars={len(own)} {r['url']}\n"
            f"TITLE: {r['title']}\n{r['body'].strip()}",
        )

    for r in read_jsonl(data / "reviews.jsonl"):
        if not _human(r["login"]):
            continue
        parts = [
            f'[review {r["state"]}] {r["repo"]} @{r["login"]} on "{r["pr_title"]}" (PR author @{r["pr_author"]}) {r["url"]}'
        ]
        if (r["body"] or "").strip():
            parts.append("BODY: " + r["body"].strip())
        for c in r["comments"]:
            hunk = "\n".join((c.get("diffHunk") or "").split("\n")[-5:])
            parts.append(
                f"  INLINE {c.get('path')}:\n  ```\n{hunk}\n  ```\n  > {(c.get('body') or '').strip()}"
            )
        if len(parts) == 1:
            parts[0] += " (no text)"
        add("reviews", r["createdAt"], "\n".join(parts))

    # Person/team mode writes comments.jsonl; repo mode splits comments_pr/comments_issue.
    comment_rows = [
        r for path in sorted(data.glob("comments*.jsonl")) for r in read_jsonl(path)
    ]
    for r in comment_rows:
        if _human(r["login"]) and (r["body"] or "").strip():
            add(
                "conversation",
                r["createdAt"],
                f'[{r["parent_kind"]} comment] {r["repo"]} @{r["login"]} on "{r["parent_title"]}" '
                f"(author @{r['parent_author']})\n{r['body'].strip()}",
            )
    for r in read_jsonl(data / "issues.jsonl"):
        if _human(r["login"]):
            add(
                "conversation",
                r["createdAt"],
                f'[issue opened] {r["repo"]} @{r["login"]} "{r["title"]}" own_words_chars={len(_own_words(r["body"]))}\n'
                f"{r['body'].strip()}",
            )
    for r in read_jsonl(data / "discussions.jsonl"):
        if _human(r["login"]):
            add(
                "conversation",
                r["createdAt"],
                f'[discussion post, {r["category"]}] {r["repo"]} @{r["login"]} "{r["title"]}"\n{(r["body"] or "").strip()}',
            )
    for r in read_jsonl(data / "discussion_comments.jsonl"):
        if _human(r["login"]) and (r["body"] or "").strip():
            add(
                "conversation",
                r["createdAt"],
                f'[discussion comment] {r["repo"]} @{r["login"]} on "{r["parent_title"]}"\n{r["body"].strip()}',
            )

    manifest = {
        m["repo"]: m
        for m in json.loads((workdir / "repos" / "manifest.json").read_text())
    }
    diff_stats: dict[str, collections.Counter] = {}
    for era in [e["name"] for e in subject["eras"]]:
        pool = [
            r
            for r in commits
            if r["era"] == era
            and r["has_stat"]
            and not r["ai_flag"]
            and min_lines <= r["insertions"] + r["deletions"] <= max_lines
            and "error" not in manifest.get(r["repo"], {"error": True})
        ]
        rng.shuffle(pool)
        by_lang: dict[str, list[dict]] = collections.defaultdict(list)
        # Classifying a commit costs one `git show`; stop scanning once the pool is large enough.
        for r in pool[:scan_limit]:
            who = r["login"] or r["author"]
            files = subprocess.run(
                [
                    "git",
                    "-C",
                    manifest[r["repo"]]["path"],
                    "show",
                    "--name-only",
                    "--format=",
                    r["sha"],
                ],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.split()
            lang = _dominant_language(files)
            if not lang or len(by_lang[lang]) >= per_language:
                continue
            if sum(x["repo"] == r["repo"] for x in by_lang[lang]) >= per_repo:
                continue
            if (
                subject["mode"] != "person"
                and sum((x["login"] or x["author"]) == who for x in by_lang[lang])
                >= per_author
            ):
                continue
            by_lang[lang].append(r)
        for lang, picked in by_lang.items():
            for r in picked:
                patch = subprocess.run(
                    [
                        "git",
                        "-C",
                        manifest[r["repo"]]["path"],
                        "show",
                        "--stat",
                        "-p",
                        "--format=%H %aI @%an%n%B",
                        r["sha"],
                    ],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    check=False,
                ).stdout
                patch = "\n".join(patch.split("\n")[:260])
                buckets[("diffs", era)].append(
                    (r["date"], f"[{lang}] {r['repo']} {r['date'][:10]}\n{patch}")
                )
        diff_stats[era] = collections.Counter(
            {lang: len(p) for lang, p in by_lang.items()}
        )

    index = ["# Corpus index", "", "| file | entries | KB |", "|---|---|---|"]
    for (kind, era), entries in sorted(
        buckets.items(), key=lambda item: (item[0][1], item[0][0])
    ):
        path = out / f"{kind}_{era}.txt"
        count = _write(path, entries)
        index.append(f"| {path.name} | {count} | {path.stat().st_size // 1024} |")
    index += ["", "## Diff sample by language", ""]
    index += [f"- {era}: {dict(counts)}" for era, counts in diff_stats.items()]
    (out / "INDEX.md").write_text("\n".join(index) + "\n")
    print("\n".join(index))


if __name__ == "__main__":
    app()
