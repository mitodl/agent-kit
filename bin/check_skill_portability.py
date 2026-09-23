#!/usr/bin/env python3
"""Fail when a shipped skill assumes a Claude-only tool with no fallback.

WHY. ``agent-kit apply agent-config.toml`` installs every skill in the
manifest's ``[skills]`` table into every detected platform — Claude Code, Pi,
Copilot, OpenCode — and every profile inherits ``universal``. A skill that
says "use the Agent tool" or "run the built-in /review" is therefore read by
agents that have no such thing: stock Pi ships no subagent tool, no
AskUserQuestion-style tool and no /review command. Those instructions then
either stall the run or, worse, get quietly skipped along with the work they
were meant to do.

RULE. A mention of a Claude-only capability (the ``CLAUDE_ONLY`` patterns
below) is allowed only in a passage that is visibly capability-conditional,
i.e. the same passage also contains a fallback marker (``FALLBACK_MARKERS``:
"if ... available", "otherwise", "fallback", "on platforms with",
"Claude-specific", "stock Pi", ...). A passage is a paragraph (blank-line
separated block, so a whole list counts as one passage and a sibling item can
carry the fallback), a heading, or the frontmatter. So

    Spawn one subagent per batch.                          -> flagged
    With a subagent facility, spawn one per batch;
    otherwise run the batches sequentially.                -> allowed

For a legitimate mention the marker heuristic can't see (e.g. a doc that is
itself about one platform), put ``<!-- portability: ok -->`` in the passage;
that is the explicit allowlist, greppable and reviewed in the diff.

SCOPE. Markdown files (SKILL.md, references/, any other .md) under each skill
directory registered in agent-config.toml's ``[skills]`` table. The Witan-owned
skills shipped inside the witan packages are not registered there and are not
checked. Scripts are not scanned: their strings are output, not instructions.

Standard library only (Python 3.11+ for tomllib) so it runs as a plain
pre-commit hook with no environment to build, like bin/gen_llms.py. Run it
from the repo root: ``./bin/check_skill_portability.py``.
"""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "agent-config.toml"

# Claude Code capabilities that agent-kit does not provide on other platforms.
CLAUDE_ONLY: dict[str, re.Pattern[str]] = {
    "AskUserQuestion tool": re.compile(r"\bAskUserQuestion\b"),
    "ToolSearch tool": re.compile(r"\bToolSearch\b"),
    "TodoWrite tool": re.compile(r"\bTodoWrite\b"),
    "ExitPlanMode tool": re.compile(r"\bExitPlanMode\b"),
    "Agent tool": re.compile(r"`?\bAgent`?\s+tool\b"),
    "Task tool": re.compile(r"`?\bTask`?\s+tool\b"),
    # Built-in slash commands; the lookbehind skips paths like .../review and
    # skills/process/code-review, the lookahead skips /reviews.
    "built-in slash command": re.compile(
        r"(?<![\w/.$}-])/(review|security-review|code-review|simplify)\b(?![\w-])"
    ),
    "subagent delegation": re.compile(r"\bsub-?agents?\b", re.IGNORECASE),
    "parallel agents": re.compile(
        r"\bparallel agents\b|\bspawn (one|an|a) agent\b", re.IGNORECASE
    ),
}

# Wording that makes a passage capability-conditional.
FALLBACK_MARKERS = re.compile(
    r"|".join(
        [
            r"\bif\b[^.;]*\b(available|installed|exists|supports?|provides?)\b",
            r"\b(where|when|with)\b[^.;]*\b(available|installed|exists|supports?|provides?)\b",
            r"\bwith an? (authorized )?subagent facility\b",
            r"\bon platforms with\b",
            r"\bwithout (a |any )?(subagent|delegation)",
            r"\botherwise\b",
            r"\bfall ?back\b",
            r"\bonly where\b",
            r"\bclaude[- ]specific\b",
            r"\bstock pi\b",
            r"<!--\s*portability:\s*ok\s*-->",
        ]
    ),
    re.IGNORECASE,
)

_HEADING = re.compile(r"^#{1,6}\s")


def passages(text: str) -> list[tuple[int, str]]:
    """Split markdown into (first line number, passage text) units."""
    out: list[tuple[int, str]] = []
    lines = text.splitlines()
    start, buf = 0, []

    def flush() -> None:
        if buf:
            out.append((start + 1, "\n".join(buf)))
        buf.clear()

    i = 0
    if lines and lines[0].strip() == "---":  # frontmatter is one passage
        end = next((j for j in range(1, len(lines)) if lines[j].strip() == "---"), None)
        if end is not None:
            out.append((1, "\n".join(lines[: end + 1])))
            i = end + 1
    for n in range(i, len(lines)):
        line = lines[n]
        if not line.strip():
            flush()
            continue
        if _HEADING.match(line):
            flush()
        if not buf:
            start = n
        buf.append(line)
        if _HEADING.match(line):
            flush()
    flush()
    return out


def violations(text: str) -> list[tuple[int, str, str]]:
    """Return (line, label, matched text) for each unconditional mention."""
    found = []
    for first_line, passage in passages(text):
        if FALLBACK_MARKERS.search(passage):
            continue
        for label, pattern in CLAUDE_ONLY.items():
            for m in pattern.finditer(passage):
                line = first_line + passage.count("\n", 0, m.start())
                found.append((line, label, m.group(0)))
    return sorted(found)


def _self_test() -> None:
    """Guard the rule itself: known-bad text flags, known-good text passes."""
    bad = [
        "Dispatch one subagent per batch in parallel.",
        "This is the shape the `Agent` tool suits.",
        "Run this session's built-in `/review <pr-url>`.",
        "Use AskUserQuestion to pick the repo.",
        "Spawn one agent per batch.\n\nOtherwise, run them sequentially.",
    ]
    good = [
        "With a subagent facility, one per batch; otherwise sequentially.",
        "If an AskUserQuestion-style tool is available, use it; otherwise ask in chat.",
        "Claude Code's built-in `/review` is a Claude-specific alternative.",
        "See skills/process/code-review/SKILL.md and repos/x/pulls/1/reviews.",
        "Spawn one subagent per batch. <!-- portability: ok -->",
        "- Spawn one agent per batch.\n- Otherwise, run them sequentially.",
    ]
    for sample in bad:
        assert violations(sample), f"self-test: should flag: {sample!r}"
    for sample in good:
        assert not violations(sample), f"self-test: should pass: {sample!r}"


def main() -> int:
    _self_test()
    skills = tomllib.loads(MANIFEST.read_text())["skills"]
    failures = 0
    for name, skill_md in sorted(skills.items()):
        skill_dir = (ROOT / skill_md).parent
        for md in sorted(skill_dir.rglob("*.md")):
            for line, label, match in violations(md.read_text()):
                failures += 1
                rel = md.relative_to(ROOT)
                print(
                    f"{rel}:{line}: {label} ({match!r}) with no fallback in the "
                    f"same passage [skill {name}]"
                )
    if failures:
        print(
            f"\n{failures} unconditional Claude-only capability mention(s). These "
            "skills install on Pi and other platforms too: make the passage "
            "capability-conditional with a stock-platform fallback ('if a "
            "subagent facility is available ... otherwise ...'), or mark a "
            "deliberate platform-specific mention with <!-- portability: ok -->. "
            "See bin/check_skill_portability.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
