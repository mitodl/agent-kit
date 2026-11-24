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

import json
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

# Packages outside the uv workspace that release the same way: the version in
# package.json, `just bump`'s copy in .bumpversion.toml, and a scriv CHANGELOG.
NODE_MEMBERS = ("packages/witan-vscode",)

# Images whose version label tracks a workspace member's release: the
# Dockerfile, the ARG that feeds org.opencontainers.image.version, and the
# member. `just bump` rewrites the ARG through that member's
# [[tool.bumpversion.files]]. The witan image's label sat at 0.8.0 through
# witan-council 0.53.0 because the bump never touched it and nothing looked.
IMAGE_LABELS = (("docker/witan.Dockerfile", "WITAN_VERSION", "mcp/servers/witan"),)

# A node member's version until its first `just bump`. Nothing has shipped, so
# there is no CHANGELOG heading for it to match; its publish workflow refuses it.
UNRELEASED = "0.0.0"

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
    bumpversion = data.get("tool", {}).get("bumpversion", {}).get("current_version")
    return check_release(
        root,
        member,
        data["project"]["name"],
        data["project"]["version"],
        bumpversion,
        "[project].version",
        "[tool.bumpversion].current_version",
    )


def check_node_member(root: Path, member: str) -> list[Problem]:
    """Return every disagreement for one package.json package."""
    package_json = root / member / "package.json"
    if not package_json.exists():
        return [Problem(member, f"no package.json at {member} — package moved?")]

    package = json.loads(package_json.read_text())
    config = root / member / ".bumpversion.toml"
    bumpversion = (
        tomllib.loads(config.read_text())["tool"]["bumpversion"]["current_version"]
        if config.exists()
        else None
    )
    return check_release(
        root,
        member,
        package["name"],
        package["version"],
        bumpversion,
        "package.json's version",
        ".bumpversion.toml's current_version",
        unreleased=package["version"] == UNRELEASED,
    )


def check_release(
    root: Path,
    member: str,
    name: str,
    version: str,
    bumpversion: str | None,
    version_field: str,
    bumpversion_field: str,
    *,
    unreleased: bool = False,
) -> list[Problem]:
    """Return every disagreement between a package's version, its bumpversion
    config and its CHANGELOG."""
    problems: list[Problem] = []
    if bumpversion is None:
        problems.append(
            Problem(
                name,
                f"no {bumpversion_field} — `just bump` cannot release this package",
            )
        )
    elif bumpversion != version:
        problems.append(
            Problem(
                name,
                f"{version_field} is {version} but "
                f"{bumpversion_field} is {bumpversion}. "
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

    if unreleased:
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
    # `just bump` writes both together (bump-my-version sets the version, then
    # `scriv collect` writes the matching heading from pending changelog.d/
    # fragments in the same recipe), so in the normal flow they're never out of
    # step to begin with. Left unchecked anyway for a changelog hand-edited
    # ahead of a release — a state this doesn't need to forbid to catch what it
    # exists to catch: the version and the record of what shipped disagreeing.
    return problems


def check_image_label(
    root: Path, dockerfile: str, arg: str, member: str
) -> list[Problem]:
    """Return a problem when ``dockerfile``'s ``ARG arg=`` default isn't ``member``'s version."""
    pyproject = root / member / "pyproject.toml"
    # check_member already reports a missing member; a second line would only repeat it.
    if not pyproject.exists():
        return []
    project = tomllib.loads(pyproject.read_text())["project"]
    image = root / dockerfile
    if not image.exists():
        return [Problem(project["name"], f"no {dockerfile} — image moved or renamed?")]
    found = re.search(rf"^ARG {arg}=(?P<version>\S+)$", image.read_text(), re.MULTILINE)
    if found is None:
        return [
            Problem(
                project["name"],
                f"{dockerfile} has no `ARG {arg}=<version>` default to label the image with",
            )
        ]
    if found.group("version") != project["version"]:
        return [
            Problem(
                project["name"],
                f"{dockerfile} labels the image {found.group('version')} "
                f"(ARG {arg}) but [project].version is {project['version']}.",
            )
        ]
    return []


@app.default
def check(root: str = ".") -> None:
    """Check every workspace member.

    Parameters
    ----------
    root
        Repo root. Defaults to the working directory.
    """
    base = Path(root).resolve()
    problems = (
        [p for member in MEMBERS for p in check_member(base, member)]
        + [p for member in NODE_MEMBERS for p in check_node_member(base, member)]
        + [p for label in IMAGE_LABELS for p in check_image_label(base, *label)]
    )

    if not problems:
        print(
            f"versions consistent across {len(MEMBERS) + len(NODE_MEMBERS)} packages "
            f"and {len(IMAGE_LABELS)} image label(s)"
        )
        return

    print("VERSION / CHANGELOG DRIFT\n", file=sys.stderr)
    for problem in problems:
        print(f"  {problem.package}: {problem.detail}\n", file=sys.stderr)
    print(
        "Release a package with `just bump <package> <major|minor|patch>`, "
        "which keeps all three in step. Add a changelog.d/ fragment first "
        "with `just changelog <package>` if there isn't one yet.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    app()
