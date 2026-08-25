#!/usr/bin/env python3
"""Generate llms.txt, llms-full.txt, and a raw-markdown mirror for the built site.

WHY. An agent consuming these docs shouldn't have to parse rendered HTML to get
plain text back out of it. The convention (https://llmstxt.org) is a compact
link index at ``/llms.txt`` plus, ideally, a raw-markdown copy of every page an
agent can fetch directly instead of the HTML. This script produces both, plus
``/llms-full.txt`` — the whole corpus concatenated into one file, for an agent
that would rather fetch once than crawl.

Nothing here is hand-maintained. The link index, its section grouping, and the
per-page descriptions are all derived from ``docs/`` and the ``nav`` already
declared in ``zensical.toml`` — the same source of truth the rendered site
uses — so there is no second copy of the site structure to keep in sync by
hand. A description is the page's own first paragraph, lightly stripped of
markdown syntax; there is no separate blurb to write or forget to update.

WHY THIS SCRIPT IS STDLIB-ONLY, UNLIKE ITS SIBLINGS IN ``bin/``. Every other
generator here runs via ``uv run --package witan-core ...``, because Read the
Docs deliberately does NOT sync the uv workspace for this build — see the note
in ``.readthedocs.yaml``: it installs zensical alone, specifically so the docs
build never has to resolve tree-sitter, fastmcp, or an omnigraph binary. This
script runs in that same build job, right after ``zensical build``, so it has
to work with nothing beyond what a bare ``python3`` on RTD's image already
has. Reaching for ``uv run --package witan-core`` here would drag the exact
dependency weight RTD's config was written to avoid back in through this side
door.

Usage:
    zensical build && ./bin/gen_llms.py    # after the HTML build, before publish
"""

from __future__ import annotations

import re
import shutil
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
SITE = REPO_ROOT / "site"
ZENSICAL_TOML = REPO_ROOT / "zensical.toml"

_HEADING_OR_BLOCK = ("#", "<", "!!!", "```", "|", "-", "*", "{")


def _first_paragraph(md_path: Path) -> str:
    """The first prose paragraph after the H1, as a single markdown-stripped line."""
    lines = md_path.read_text().splitlines()
    seen_h1 = False
    body: list[str] = []
    for line in lines:
        stripped = re.sub(r"^>+\s*", "", line.strip())
        if not seen_h1:
            if stripped.startswith("# "):
                seen_h1 = True
            continue
        if not stripped:
            if body:
                break
            continue
        if stripped.startswith(_HEADING_OR_BLOCK):
            if body:
                break
            continue
        if not body and line.startswith((" ", "\t")):
            # Wrapped continuation of a skipped bullet/blockquote, e.g. a
            # `- Related: …` metadata line that wraps onto an unindented-looking
            # but source-indented second line. Only applies before real prose
            # has started — an indented line inside a found paragraph is left
            # alone.
            continue
        body.append(stripped)

    para = " ".join(body)
    para = re.sub(r"`([^`]*)`", r"\1", para)
    para = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", para)
    para = re.sub(r"\*\*([^*]+)\*\*", r"\1", para)
    para = re.sub(r"\s+", " ", para).strip()
    if len(para) > 160:
        para = para[:160].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return para


def _flatten(nodes: list, trail: list[str]) -> list[tuple[str, str]]:
    """(breadcrumb title, docs-relative .md path) for every leaf under `nodes`."""
    leaves: list[tuple[str, str]] = []
    for node in nodes:
        ((title, value),) = node.items()
        if isinstance(value, str):
            crumb = " — ".join([*trail, title]) if trail else title
            leaves.append((crumb, value))
        else:
            leaves.extend(_flatten(value, [*trail, title]))
    return leaves


def _sections(nav: list) -> list[tuple[str, list[tuple[str, str]]]]:
    """One (section title, leaves) pair per top-level nav entry, in nav order."""
    sections = []
    for node in nav:
        ((title, value),) = node.items()
        leaves = [(title, value)] if isinstance(value, str) else _flatten(value, [])
        sections.append((title, leaves))
    return sections


def _mirror_markdown(sections: list[tuple[str, list[tuple[str, str]]]]) -> None:
    """Copy every page's source markdown to the same relative path under site/."""
    for _title, leaves in sections:
        for _crumb, relpath in leaves:
            dest = SITE / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(DOCS / relpath, dest)


def _write_llms_txt(
    site_name: str,
    site_description: str,
    site_url: str,
    sections: list[tuple[str, list[tuple[str, str]]]],
) -> None:
    base = site_url.rstrip("/")
    lines = [f"# {site_name}", "", f"> {site_description}", ""]
    for title, leaves in sections:
        lines.append(f"## {title}")
        lines.append("")
        for crumb, relpath in leaves:
            desc = _first_paragraph(DOCS / relpath)
            lines.append(f"- [{crumb}]({base}/{relpath}): {desc}")
        lines.append("")
    (SITE / "llms.txt").write_text("\n".join(lines).rstrip() + "\n")


def _write_llms_full_txt(
    site_name: str,
    site_url: str,
    sections: list[tuple[str, list[tuple[str, str]]]],
) -> None:
    base = site_url.rstrip("/")
    parts = [f"# {site_name} — full corpus\n"]
    for _title, leaves in sections:
        for _crumb, relpath in leaves:
            # No injected heading: every page already opens with its own H1,
            # so one more here would just stack two titles on top of it.
            parts.append(f"<!-- {base}/{relpath} -->\n\n")
            parts.append((DOCS / relpath).read_text().rstrip() + "\n\n---\n\n")
    (SITE / "llms-full.txt").write_text("".join(parts).rstrip() + "\n")


def _check_nav_matches_docs(sections: list[tuple[str, list[tuple[str, str]]]]) -> None:
    """Every page under docs/ must be in the nav, and vice versa.

    llms.txt is only as complete as the nav it's built from. A page added to
    docs/ and forgotten in zensical.toml's nav is invisible to it (and to the
    rendered site, for the same reason); a nav entry with no file behind it
    would otherwise surface as a bare FileNotFoundError deep in
    ``_mirror_markdown``. Catch both here, together, with a message that says
    which file and which fix.
    """
    on_disk = {
        str(p.relative_to(DOCS)) for p in DOCS.rglob("*.md") if "_data" not in p.parts
    }
    in_nav = {relpath for _title, leaves in sections for _crumb, relpath in leaves}

    missing_from_nav = sorted(on_disk - in_nav)
    missing_from_disk = sorted(in_nav - on_disk)
    if not missing_from_nav and not missing_from_disk:
        return

    lines = ["docs/ and zensical.toml's nav disagree:"]
    if missing_from_nav:
        lines.append("  in docs/ but not in nav (add a nav entry, or delete the page):")
        lines += [f"    {p}" for p in missing_from_nav]
    if missing_from_disk:
        lines.append("  in nav but no file on disk (fix the path, or drop the entry):")
        lines += [f"    {p}" for p in missing_from_disk]
    sys.exit("\n".join(lines))


def main() -> None:
    if not SITE.is_dir():
        sys.exit(f"{SITE} does not exist — run `zensical build` first.")

    config = tomllib.loads(ZENSICAL_TOML.read_text())
    project = config["project"]
    site_name, site_description = project["site_name"], project["site_description"]
    site_url, nav = project["site_url"], project["nav"]

    sections = _sections(nav)
    _check_nav_matches_docs(sections)
    _mirror_markdown(sections)
    _write_llms_txt(site_name, site_description, site_url, sections)
    _write_llms_full_txt(site_name, site_url, sections)

    page_count = sum(len(leaves) for _title, leaves in sections)
    print(f"wrote llms.txt, llms-full.txt, and {page_count} mirrored markdown pages")


if __name__ == "__main__":
    main()
