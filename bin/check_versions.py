#!/usr/bin/env -S uv run --quiet --package witan-core --extra cli python
"""Fail when a package's version, its bumpversion config, and its CHANGELOG disagree.

WHY. Every workspace member publishes to PyPI on a push to main touching its own
``pyproject.toml`` (``.github/workflows/publish-*.yml``), so the version bump IS
the release trigger. Three things have to agree for that release to be
meaningful, and nothing was checking they did:

  ``[project].version``                  what gets published
  ``[tool.bumpversion].current_version`` what the bump tool searches for
  ``CHANGELOG.md``'s newest heading      what a reader is told shipped

They had drifted in three of five packages — witan-core's bumpversion config
said 0.9.0 against a real 0.15.0, witan-council 0.10.0 against 0.11.5,
witan-code 0.11.1 against 0.12.3 — while every CHANGELOG was correct. That
pattern says the tool was configured once and then never used: versions were
hand-edited, `bump-my-version` silently searched for a string that no longer
existed, and nobody found out because nothing looked.

This is the thing that looks. It runs in CI on every PR, so drift is caught
whether or not the bump went through ``just bump``.

WHAT IT DOES NOT DO: check that the version was bumped *at all* for a given
change. That is a judgement call about whether a diff is user-visible, and a
rule mechanical enough to enforce would be wrong often enough to be routed
around. This only enforces internal consistency — cheap, unambiguous, and
exactly what rotted.
"""

import re
import sys
import tomllib
from pathlib import Path
from typing import NamedTuple

import cyclopts

# Workspace members, as declared in the root pyproject's [tool.uv.workspace].
# Listed rather than globbed so a new member has to be added deliberately —
# and so this fails loudly if one is moved rather than silently skipping it.
MEMBERS = (
    "packages/witan-core",
    "packages/agent-config-kit",
    "packages/agent-kit",
    "mcp/servers/witan",
    "mcp/servers/witan-code",
)

# `## [0.16.0] - 2026-08-10` — the Keep a Changelog release heading. Anchored to
# a digit so an `## [Unreleased]` section is skipped rather than read as the
# newest release.
RELEASE_HEADING = re.compile(r"^## \[(?P<version>[0-9][^\]]*)\]", re.MULTILINE)

app = cyclopts.App(
    name="check-versions",
    help="Assert version, bumpversion config, and CHANGELOG agree per package.",
)


class Problem(NamedTuple):
    """One disagreement, with enough detail to fix it without looking further."""

    package: str
    detail: str


def check_member(root: Path, member: str) -> list[Problem]:
    """Return every disagreement for one workspace member."""
    pyproject = root / member / "pyproject.toml"
    if not pyproject.exists():
        return [Problem(member, f"no pyproject.toml at {member} — member moved?")]

    data = tomllib.loads(pyproject.read_text())
    name = data["project"]["name"]
    version = data["project"]["version"]
    problems: list[Problem] = []

    bumpversion = data.get("tool", {}).get("bumpversion", {}).get("current_version")
    if bumpversion is None:
        problems.append(
            Problem(
                name,
                "no [tool.bumpversion].current_version — `just bump` cannot "
                "release this package",
            )
        )
    elif bumpversion != version:
        problems.append(
            Problem(
                name,
                f"[project].version is {version} but "
                f"[tool.bumpversion].current_version is {bumpversion}. "
                "bump-my-version searches for the latter, so it would find "
                "nothing and silently do nothing. Set them equal.",
            )
        )

    changelog = root / member / "CHANGELOG.md"
    if not changelog.exists():
        problems.append(
            Problem(name, f"no CHANGELOG.md — {member}/CHANGELOG.md is required")
        )
        return problems

    text = changelog.read_text()
    headings = [m.group("version") for m in RELEASE_HEADING.finditer(text)]
    if not headings:
        problems.append(
            Problem(name, "CHANGELOG.md has no `## [<version>]` release heading")
        )
    elif version not in headings:
        problems.append(
            Problem(
                name,
                f"CHANGELOG.md has no entry for the released version {version} "
                f"(newest heading is {headings[0]}). Whatever is about to be "
                "published is undocumented.",
            )
        )
    # DELIBERATELY NOT CHECKED: that the newest heading equals the version.
    # A changelog entry ahead of the version is the normal mid-release state —
    # `just bump` requires the entry to exist BEFORE it will move the version,
    # so between writing the entry and running the bump every package is
    # legitimately in exactly that shape. Flagging it would make the pre-flight
    # check inside `just bump` unsatisfiable, and would fail CI on any PR that
    # documents a release in one commit and cuts it in the next.
    return problems


@app.default
def check(root: str = ".") -> None:
    """Check every workspace member.

    Parameters
    ----------
    root
        Repo root. Defaults to the working directory.
    """
    base = Path(root).resolve()
    problems = [p for member in MEMBERS for p in check_member(base, member)]

    if not problems:
        print(f"versions consistent across {len(MEMBERS)} packages")
        return

    print("VERSION / CHANGELOG DRIFT\n", file=sys.stderr)
    for problem in problems:
        print(f"  {problem.package}: {problem.detail}\n", file=sys.stderr)
    print(
        "Release a package with `just bump <package> <major|minor|patch>`, "
        "which keeps all three in step. See the justfile recipe for why the "
        "CHANGELOG entry is written first.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    app()
