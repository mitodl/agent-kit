---
name: renovate-security-triage
description: >
  Summarize open Renovate dependency-update PRs across a GitHub org, narrowed to
  the repos you personally worked in recently, ranked by descending security
  urgency using real GitHub Advisory Database severity, CVSS and EPSS data. Use
  this skill when asked what dependency updates need attention, which Renovate
  PRs are security-critical, to triage a Renovate backlog, to review dependency
  bot PRs across mitodl, or for questions like "what security updates am I
  sitting on", "any urgent CVEs in my repos", "summarize my Renovate PRs". It
  reports only and never merges, comments on, or otherwise modifies anything on
  GitHub. Hand off to the dependency-updates skill to actually apply a fix.
license: BSD-3-Clause
metadata:
  category: process
---

# Renovate Security Triage

Turns an org-wide flood of Renovate PRs into a short, ranked list of the ones
that matter to you. The pattern is
**scope → enumerate → enrich → score → rank → report**, with five scripts doing
the mechanical work and the model supplying the judgment that advisory metadata
cannot.

Calibration from a real mitodl run: **436** open Renovate PRs org-wide across
**78** repos → **175** PRs in the user's **21** active repos → **36**
security-relevant, of which **2** critical and **15** high. The active-repo
filter is what makes this readable; without it the report is noise.

## Read-only, without exception

This skill **never mutates GitHub**. No merging, no commenting, no review
requests, no label edits, no Dependency Dashboard checkbox ticking, no
`gh api --method POST/PATCH/PUT/DELETE`, no GraphQL `mutation`. Every script
here issues queries only.

If the user wants a PR *applied*, that is the **`dependency-updates`** skill's
job — name it and hand off. Do not add write capability to this skill, and do
not port the "optionally act" phase from the sibling `github-pr-triage` skill.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/active-repos.sh` | Repos in the org the user contributed to in the window |
| `scripts/fetch-renovate-prs.sh` | Open Renovate PRs, filtered to those repos |
| `scripts/enrich-renovate-prs.sh` | Per-PR body/files/checks + parse Renovate's update table |
| `scripts/advisory-lookup.sh` | Resolve GitHub Advisory Database records; attach severity/CVSS/EPSS |
| `scripts/classify-renovate-prs.sh` | Assign urgency tiers and sort |

The `.sh` files are orchestration only (argument parsing, `gh` calls,
parallelism). The heavy data transforms live as standalone jq programs in
`scripts/jq/`, one per stage, run with `jq -f`; shared shell helpers are in
`scripts/lib.sh` and the fixed file locations in `scripts/paths.sh`.

## Never pass file paths

Run every command below **exactly as written**. The scripts hand JSON to each
other through fixed locations they compute themselves (`scripts/paths.sh`,
defaulting under `~/.cache/renovate-security-triage/`) and print where they
wrote. Each accepts explicit paths for ad-hoc use, but the pipeline must not
supply them.

This is not a style preference. A path typed on the command line changes the
command string, and a bash command approved once only stays approved while its
text is stable. **In particular: do not substitute a session scratchpad path.**
Any general instruction you carry about preferring a scratchpad directory over
`/tmp` does not apply here — these files are not yours to place, and rewriting
them re-prompts the user on every single run. To relocate them, set
`RENOVATE_TRIAGE_DIR` in the environment instead.

Tunable flags (`--since`, `--user`, `--min-contributions`, `--author`,
`--all-repos`) are fine — append them to the command as-is.

---

## Phase 0 — Settle the scope

Resolve before running anything; ask only if genuinely ambiguous:

- **Org** — defaults to `mitodl` in this context. One org per run.
- **Window** — defaults to 365 days. GitHub caps its contributions API at a
  one-year span, so an earlier `--since` is clamped with a warning.
- **Whose activity** — defaults to the authenticated user. `--user LOGIN`
  measures someone else, but only their *public* contributions are visible.
- **Bot** — defaults to Renovate (`--author app/renovate`). Use
  `--author app/dependabot` for Dependabot.

---

## Phase 1 — Find the active repos

```bash
./skills/process/renovate-security-triage/scripts/active-repos.sh mitodl
# tunable:
./skills/process/renovate-security-triage/scripts/active-repos.sh mitodl --min-contributions 5
```

Primary source is GraphQL `contributionsCollection` — the only surface covering
commits, PRs, reviews **and** issues in one date-scoped call. It is unioned with
`gh search prs --author` / `--reviewed-by` to catch work the contribution graph
misses (it counts only default-branch commits).

`--min-contributions` excludes drive-by fixes. Repos admitted *only* by the
search union are kept regardless, since search membership is itself evidence of
involvement.

---

## Phase 2 — Enumerate Renovate PRs

```bash
./skills/process/renovate-security-triage/scripts/fetch-renovate-prs.sh mitodl
```

One org-wide `gh search prs` call, then filtered to the active-repo set. Pass
`--all-repos` to skip the filter.

**The search API caps at 1000 results.** The script warns when it hits the cap;
if you see that warning, say so in the report rather than presenting a truncated
list as complete.

---

## Phase 3 — Enrich and parse

```bash
./skills/process/renovate-security-triage/scripts/enrich-renovate-prs.sh
```

8-way parallel `gh pr view`, then Renovate's PR body is parsed into structured
fields: `updates[]` (package, from, to, bump), `ghsa_ids`, `cve_ids`,
`ecosystems`, `kind`, `checks`, `grouped`. See
[references/renovate-pr-anatomy.md](references/renovate-pr-anatomy.md) for the
body formats this parses and the ones it deliberately yields nothing for.

A PR with zero parsed `updates` is usually **not** a parse failure —
`kind: "lockfile_maintenance"` and `kind: "pin"` legitimately bump no package
versions. Check `kind` before treating an empty `updates` as a bug.

---

## Phase 4 — Score against the advisory database

```bash
./skills/process/renovate-security-triage/scripts/advisory-lookup.sh
```

Renovate embeds the GHSA ids it is fixing directly in the PR body, so severity
comes from **looking those ids up**, not from guessing by package name. Each PR
gains `advisories[]`, `max_severity`, `max_cvss`, `max_epss_percentile`,
`advisory_source`, `unresolved_ghsa_ids`.

Ad-hoc single lookups, useful while writing the report:

```bash
./skills/process/renovate-security-triage/scripts/advisory-lookup.sh --ghsa GHSA-g6cj-pr64-35w5
./skills/process/renovate-security-triage/scripts/advisory-lookup.sh --package PIP cryptography
```

---

## Phase 5 — Rank

```bash
./skills/process/renovate-security-triage/scripts/classify-renovate-prs.sh
```

It prints the `summary` counts and the path it wrote. **Read that file with the
Read tool** — do not `cat` or `jq` it in bash.

| Tier | `urgency` | Meaning |
|------|-----------|---------|
| 0 | `critical` | CRITICAL advisory |
| 1 | `high` | HIGH advisory |
| 2 | `moderate` | MODERATE advisory |
| 3 | `low` | LOW advisory |
| 4 | `security_unscored` | Security-relevant but no advisory resolved — needs a human look |
| 5 | `major_non_security` | Major bump; breaking-change risk, not security risk |
| 6 | `routine` | Minor / patch / digest / pin / lockfile maintenance |

Within a tier: CVSS descending, then EPSS percentile descending, then oldest PR
first. Full rationale and worked examples in
[references/urgency-rubric.md](references/urgency-rubric.md).

---

## Phase 6 — Report

Summary line, then **one table per populated tier** for tiers 0–4, most urgent
first, then the prose sections. Tiers 5–6 are counts only.

### The table format is not negotiable

Every populated tier renders as a markdown table. The rules below exist because
a real run drifted — High came out as stacked `PR: …` records and Low as a prose
paragraph, because the old template stopped after the first tier.

- **A tier with one row is still a table.** Never prose, never a bullet list,
  never stacked `Key: value` blocks, no matter how few rows.
- **Same seven columns, same order, every tier:**
  `PR | Package | Change | CVSS | EPSS | Advisory | Notes`.
- **Exactly seven cells per row** (eight `|`). A missing value is `—`, never an
  omitted cell — dropping the EPSS cell on some rows mangles the whole table.
- **One PR per row.** Never fold several PRs into one row or one sentence.
- **No markdown links in any cell.** Cells are plain text. The terminal renders
  `[text](url)` as `text (url)`, so a linked cell costs ~55 extra columns and
  pushes the table past the width where it still renders as a table.
  - PR cell: `repo#number` — `open-discussions#4444`.
  - Advisory cell: one short id plus what the vulnerability *is* —
    `GHSA-33f9 prototype pollution`. More than one? Append `(+N more)`; do not
    list them.
  - EPSS cell: the bare percentile number — `82` — or `—`.
- **Width caps**: Package ≤ 20, Change ≤ 16 (`7.0.5→9.0.6`, `≥48→≥50`),
  Advisory ≤ 40, Notes ≤ 45. Anything longer goes in the prose sections, not in
  a cell you truncate. Those caps land the widest table around 150 rendered
  columns; the linked-cell version it replaces was 220+, which is where the
  render broke.
- Directly under each table, one **`Links:`** line carrying that tier's URLs.

### Worked example

Row counts are abbreviated here for brevity — a real report lists **every** PR in
tiers 0–4, one row each. A tier-4 table follows the same seven columns under
`### ⚪ Unscored (N)`, with `—` in CVSS and EPSS.

```markdown
**41 security-relevant Renovate PRs in your 22 active mitodl repos**
(2 critical, 17 high, 19 moderate, 3 low) — 25 blocked by failing checks or
conflicts. 151 non-security updates (57 major, 94 routine) not shown.

### 🔴 Critical (2)
| PR | Package | Change | CVSS | EPSS | Advisory | Notes |
|----|---------|--------|------|------|----------|-------|
| open-discussions#4444 | `immer` | 7.0.5→9.0.6 | 9.8 | 82 | GHSA-33f9 prototype pollution | runtime state lib, reachable |
| ol-keycloakify#174 | `@vitest/browser` | 4.1.9→4.1.10 | 9.4 | — | GHSA-p63j file-access bypass | **devDependency** — test-only |

Links: [open-discussions#4444](url) · [GHSA-33f9-j839-rf8h](url) · [ol-keycloakify#174](url) · [GHSA-p63j-vcc4-9vmv](url)

### 🟠 High (17)
| PR | Package | Change | CVSS | EPSS | Advisory | Notes |
|----|---------|--------|------|------|----------|-------|
| micromasters#5497 | `urllib3` | 1.26.5→2.7.0 | 8.9 | 85 | GHSA-2xpw decompression bomb (+8 more) | runtime HTTP; major, 2 checks failing |
| mit-learn#3741 | `cryptography` | ≥48→≥50 | 8.7 | 9 | GHSA-jwv3 cert path DoS (+1 more) | runtime TLS; 13 checks green |

Links: [micromasters#5497](url) · [GHSA-2xpw-w6gg-jr37](url) · [mit-learn#3741](url) · [GHSA-jwv3-5hgf-82ww](url)

### 🟡 Moderate (19)
| PR | Package | Change | CVSS | EPSS | Advisory | Notes |
|----|---------|--------|------|------|----------|-------|
| mitxpro#3777 | `wagtail` | 6.4.2→7.0.8 | 6.4 | — | GHSA-p4v8 admin bypass (+7 more) | needs an authed CMS user; green |

Links: [mitxpro#3777](url) · [GHSA-p4v8-rw59-93cq](url)

### 🔵 Low (3)
| PR | Package | Change | CVSS | EPSS | Advisory | Notes |
|----|---------|--------|------|------|----------|-------|
| open-discussions#4438 | `webpack` | 5.94→5.104.1 | 3.7 | — | GHSA-4mrq buildHttp SSRF | only with HttpUriPlugin |
| mitxonline#3765 | `@babel/core` | 7.28→7.29.6 | 3.2 | — | GHSA-67hx sourceMappingURL read | build-time only |
| open-discussions#4455 | `@babel/core` | 7.28→7.29.6 | 3.2 | — | same advisory | build-time only; green |

Links: [open-discussions#4438](url) · [mitxonline#3765](url) · [open-discussions#4455](url) · [GHSA-67hx-95hh-mh2c](url)
```

### Prose sections after the tables

Two named sections. This is where the judgment that a 45-char Notes cell cannot
hold belongs — do not widen a cell to fit it.

**What the scoring got wrong** — the corrections only you can make:

- **Blast radius the score cannot see.** A CVSS 9.4 in a devDependency
  (`@vitest/browser`) is not a production exposure; a CVSS 7.5 in a runtime web
  framework is. This is the single most valuable thing you add.
- **`needs_range_check: true`** — the tier came from a package-wide fallback
  query, so applicability to the pinned version is unproven. Say which ranges
  you checked and which advisories therefore do not apply.
- **`unresolved_ghsa_ids`** — Renovate cited an advisory GitHub cannot resolve.
  The vulnerability is real; point at the upstream advisory.
- **Grouped PRs** — name the member carrying the severity rather than letting the
  max tar every package in the group.

**Suggested order** — what to land first, what is gated behind failing CI and
needs engineering work, and what to close rather than merge. `blocked` does not
change urgency but it changes what the user does next.

Then: tiers 5–6 as counts, never enumerated, and **`dependency-updates`** named
as the next step for anything the user wants applied.

Everywhere — cells and prose alike — **say what the vulnerability actually is**,
not just its score. "Prototype pollution reachable from user input" beats
"CVSS 9.8".

---

## Where the scripts stop and you start

The pipeline is deterministic about *severity* and deliberately silent about
*applicability*. You must supply:

1. **Version-range applicability.** There is no semver comparator in jq. When
   `needs_range_check` is set, compare each advisory's `vulnerableVersionRange`
   and `firstPatchedVersion` against the update's `from` version yourself. A
   `1.2.3 → 1.9.0` bump may cross no patch boundary at all.
2. **Reachability and blast radius.** Runtime vs dev, direct vs transitive,
   internet-facing vs internal tooling. `ecosystems`, `files` and `updates`
   carry the evidence; the ranking deliberately does not fold this in, so that
   the sort stays explainable.
3. **Tier 4 adjudication.** Read the body, follow the advisory link, decide.
4. **Grouped PRs.** A grouped PR inherits its members' max severity, which can
   overstate the rest of the group. Name which member carries the risk.
5. **Unscorable ecosystems.** Helm charts, `Dockerfile` `FROM` bumps and
   docker-compose images have no advisory ecosystem and come back with
   `ecosystems: []`. They are ranked as routine but may still carry a CVE —
   check the upstream image or chart release notes before calling them safe.

## Footguns

- **Markdown links inside table cells break the render.** The terminal prints
  `[text](url)` as `text (url)`, so a linked PR cell costs ~76 columns and a
  linked advisory cell ~90 — width the markdown source does not show you. Two of
  those per row push the table past the terminal width, and the renderer
  silently abandons the box drawing and stacks each row as
  `PR: … / Package: …` records instead. Verified on the High tier of a real run,
  where Critical (~220 columns) rendered as a table and High did not. Keep cells
  plain text; put URLs in the per-tier `Links:` line.
- **`[SECURITY]` in the title is not a reliable signal.** Verified counterexample:
  `mitodl/ol-keycloakify#174` is titled `chore(deps): update dependency
  @vitest/browser to v4.1.10` and carries a **CVSS 9.4** advisory. Detection keys
  off body GHSA ids too, which is why that PR ranks tier 0.
- **Labels are useless here.** Every Renovate PR sampled in mitodl had an empty
  label list. Never classify on labels.
- **CVSS lives in three fields.** Recent advisories populate only
  `cvssSeverities.cvssV4`; older ones only the legacy `cvss`/`cvssV3`. The rest
  read `0.0`, not null. `advisory-lookup.sh` takes the max of all three.
- **`gh api graphql` exits non-zero and prints the `{data, errors}` envelope to
  stdout** when GraphQL reports an error, so a `--jq` filter never runs. The
  scripts validate the payload instead of trusting the exit status.
- **Renovate cites advisory ids GitHub cannot resolve** (seen on wagtail's
  `CVE-2026-54259`…`54263`). Those PRs fall back to a package-wide query and are
  tagged `needs_range_check`.
- **`compgen` is missing from some bash builds** (nixpkgs bash 5.3 in
  non-interactive mode). These scripts use `nullglob` instead; the sibling
  `github-pr-triage/scripts/enrich-prs.sh` still uses `compgen -G` and silently
  returns `[]` on such systems.
