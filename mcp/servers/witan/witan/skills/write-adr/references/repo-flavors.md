# Where an ADR goes, and which flavor it is written in

**The location is fixed: `docs/adr/`.** The *flavor* — how the record is
shaped — is not, and this file is mostly about that. Two live flavors exist in
mitodl today and they differ in their metadata lines, so writing the wrong one
produces a file that looks subtly foreign in its own directory.

## Step 1 — Resolve which `docs/`

`docs/adr/`, under the nearest ancestor of the code that already has a `docs/`
directory — the repository root unless a separately-released package owns its
own. This repo is the case that matters: witan publishes to PyPI on its own
cadence and keeps its ADRs at `mcp/servers/witan/docs/adr`, where a rule
saying "repository root" would have scattered them.

```bash
d=$(dirname "<a file this decision governs>")
while [ "$d" != "." ] && [ "$d" != "/" ]; do
  [ -d "$d/docs" ] && { echo "$d"; break; }
  d=$(dirname "$d")
done
```

The same resolution [`write-spec`](../../write-spec/references/repo-locations.md#step-1--resolve-the-root-which-is-not-always-the-repository)
uses, so a spec and the ADRs extracted from it land as siblings.

**Do not write into another directory because one already exists.** A repo
with its decisions somewhere else — `docs/decisions/`, or an ADR stranded in a
design folder, as `ol-data-platform/docs/design/adr_embedding_compute_strategy.md`
is — gets a `docs/adr/` for new records, and the old ones stay put. Read them;
never add to them.

To find those, so you can read and cross-reference them:

```bash
git ls-files | grep -iE '(^|/)(adr|adrs|decisions?)/' | sed 's|/[^/]*$||' | sort -u
```

## Step 2 — Discard generated mirrors

A directory of ADRs may be a build output. Writing there is silently
overwritten by the next docs build.

```bash
head -5 "$dir"/[0-9]*.md 2>/dev/null | grep -q 'MIRRORED FILE\|DO NOT EDIT' && skip
```

agent-kit's `docs/explanation/decisions/` is exactly this: every file opens
with a banner emitted by `MIRROR_BANNER` in `bin/gen_docs.py`, reading
`MIRRORED FILE — DO NOT EDIT HERE. Edit mcp/servers/witan/docs/adr/... instead`.
The check needs no knowledge of the generator.

Belt and braces: a directory appearing as a destination in `bin/gen_*.py`, or
under `nav` in `zensical.toml` or `mkdocs.yml`, is also suspect.

## Step 3 — Fingerprint from the newest files

```bash
# Newest by COMMIT time, not mtime: mtime reflects when the file was last
# checked out or touched locally, which has nothing to do with authorship order.
for f in $(for g in "$dir"/[0-9]*.md; do
             printf '%s\t%s\n' "$(git log -1 --format=%ct -- "$g")" "$g"
           done | sort -rn | head -3 | cut -f2); do
  # Search the head, not the first few lines: metadata can sit below a
  # blockquote note, as it does in witan ADR 0008.
  if   head -30 "$f" | grep -qm1 '^\*\*Status:\*\*'; then flavor=ol-infra
  elif head -30 "$f" | grep -qm1 '^- Status:';        then flavor=witan
  else flavor=unknown
  fi
done
```

**Newest, not a majority vote.** A vote over ol-infrastructure's 13 reproduces
the 2025 shape its own authors have since abandoned — the three newest dropped
`## Implementation Notes`, `## Notes`, effort estimates and risk levels.

Both refinements are there because the naive version got it wrong in testing:
`ls -t` nominated an ADR that had merely been touched, and a six-line window
missed a metadata block sitting under a blockquote.

| Signal | Flavor |
|---|---|
| `^\*\*Status:\*\*` | ol-infrastructure |
| `^- Status:` | witan |
| neither | unknown — read the newest file and mirror what it does |

## The two flavors

### ol-infrastructure

```markdown
# 0013. APISIX Leaves Bearer Tokens to MITx Online and MIT Learn

**Status:** Accepted
**Date:** 2026-09-30
**Deciders:** tmacey
**Technical Story:** Rootly INC-10, ol-infrastructure#4810 (reverted)
```

Four bolded metadata lines. Body per
[`adr-template.md`](adr-template.md). Closes with a `---`, a
`**Review History:**` table, and `**Last Updated:**`.

### witan (`mcp/servers/witan/docs/adr/`)

```markdown
# 11. Witan UI read transport: the UI is an MCP client, same-origin, in both modes

- Status: Accepted, amended 2026-09-22 (`memory_contradictions` joins the bound set)
- Date: 2026-09-17
- Deciders: witan platform owners
- Tracking: task `tk-decide-the-witan-ui-read-transport-...`, project `wp-cross-platform-witan-gui-11c03d`
- Supersedes: —
- Related: `docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md` (...)
```

Differences that matter when writing one:

- Metadata is a **bullet list**, not bolded lines.
- The H1 number is **unpadded** (`# 11.`) while the filename is padded
  (`0011-...`).
- Extra fields: `- Tracking:` carrying witan slugs, `- Supersedes:`,
  `- Related:` with inline explanations of each link.
- **No Review History table and no `**Last Updated:**`.**
- **Historical** revisions are `## Amendment (YYYY-MM-DD): <what>` or
  `## Addendum (YYYY-MM-DD) — <what>` headings after `## Consequences`, with
  the `- Status:` line extended to mention them. Read these — on a record that
  has them the original `## Decision` is often the stale half. **Do not write
  new ones and do not convert the existing ones:** an accepted ADR is immutable
  and a changed decision is superseded, but rewriting history to match the
  current rule destroys the record of what people knew and when.
- Universal body headings are the Nygard three: `## Context` (often with a
  `### Forces` subsection), `## Decision`, `## Consequences`. Occasional:
  `## Alternatives considered`, `## Rollout`, `## Implementation`.

## No `docs/adr/` yet

Create it, at the root resolved in Step 1. Flat, numbered files in one
directory — no per-work-item subdirectory. Do not ask: the convention is the
answer to that question.

Seed it in the same change with `template.md` and a `README.md` carrying the
index table (generate it with `scripts/adr-index.sh --print`), so the first ADR
arrives as an established convention rather than a stray file.

Use the ol-infrastructure flavor as the default for a new directory: it is the
one with a checked-in template and a written guide behind it.

## Repository-specific obligations

### agent-kit: a new ADR is three edits, not one

1. The source: `mcp/servers/witan/docs/adr/NNNN-*.md`.
2. The regenerated mirror: run `just docs-gen`, which writes
   `docs/explanation/decisions/NNNN-*.md`. **Skipping this fails CI** — the
   `docs` job runs `just docs-check`, which exits 1 on any diff.
3. A nav entry in `zensical.toml` under `"Decisions (ADRs)"`. A nav entry with
   no matching file, or a file with no entry, fails the docs build with a plain
   traceback.

Note that `_mirror_adrs` in `bin/gen_docs.py` strips YAML frontmatter before
prepending its banner, so an `applies_to` block stays out of the published
page. That behavior is load-bearing for this skill; if the mirrored page ever
shows a stray `---` or raw `applies_to:` text, that stripping has regressed.

### ol-infrastructure

The index in `docs/adr/README.md` is hand-maintained and already disagrees with
the files on several rows. Add your row; leave the pre-existing drift alone and
say you did. See `scripts/adr-index.sh`.
