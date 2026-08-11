# Urgency Rubric

Why the tiers are what they are, and where the deterministic ranking stops.

## The tiers

| Tier | `urgency` | Trigger |
|------|-----------|---------|
| 0 | `critical` | `max_severity == "CRITICAL"` |
| 1 | `high` | `max_severity == "HIGH"` |
| 2 | `moderate` | `max_severity == "MODERATE"` |
| 3 | `low` | `max_severity == "LOW"` |
| 4 | `security_unscored` | `[SECURITY]` in title **or** a GHSA id in the body, but no advisory resolved |
| 5 | `major_non_security` | `bump == "major"`, no advisory |
| 6 | `routine` | everything else |

Tiebreak inside a tier: **CVSS desc → EPSS percentile desc → oldest PR first**.

Severity is the primary key rather than CVSS because GitHub assigns `severity`
to every advisory but populates CVSS inconsistently (see below). Sorting on CVSS
first would scatter unscored advisories to the bottom of the list regardless of
how bad they are.

## Why tier 4 sits above plain updates

An unscorable security PR is *unknown*, not *unimportant*. Ranking it below a
routine minor bump would bury exactly the cases needing human attention. It sits
below LOW because a resolved LOW advisory is a known quantity, and above tier 5
because breaking-change risk is not security risk.

In the reference mitodl run this bucket emptied to **zero** once the package
fallback was added — every security-relevant PR resolved to a real severity.

## CVSS: three fields, one number

GitHub splits CVSS across `cvss.score` (legacy), `cvssSeverities.cvssV3.score`,
and `cvssSeverities.cvssV4.score`, and leaves the unused ones at **`0.0`, not
`null`**. Recent advisories carry only v4; older ones only v3.

```text
GHSA-jwv3-5hgf-82ww   cvss 0.0   v3 0.0   v4 8.7    <- v4-only (2026)
GHSA-537c-gmf6-5ccf   cvss 7.5   v3 7.5   v4 0.0    <- v3-only (older)
```

`advisory-lookup.sh` takes the max of all three and maps `0` to `null`. Reading
`cvss.score` alone would score most 2026 advisories as `0.0` and sort them last.

## EPSS as the second key

`advisory.epss` gives `percentage` (probability of exploitation in 30 days) and
`percentile` (rank against all CVEs). It is a better *urgency* signal than CVSS,
which measures severity-if-exploited. Two real rows show why both keys matter:

| PR | CVSS | EPSS pct | Read |
|----|------|----------|------|
| `open-discussions#4436` lodash | 8.1 | **97%** | Lower CVSS, near-certain exploitation — do this first |
| `odl-video-service#1574` cryptography | 8.2 | **7%** | Higher CVSS, needs an exotic S/MIME oracle setup |

CVSS stays the primary tiebreak (it is populated more often), but **say the EPSS
percentile out loud in the report** whenever it is high — it frequently
contradicts the CVSS ordering and is the more actionable number.

## Version-range applicability — model judgment

The scripts do **not** decide whether the currently pinned version is actually
vulnerable. There is no semver comparator in jq, and hand-rolling one for `>=49,<50`,
`~1.2`, `^2.0.0-beta.3` and digests would be wrong in the cases that matter.

When `advisory_source == "pr_body"`, Renovate already determined the PR closes
those advisories — applicability is established and no check is needed.

When `needs_range_check == true` (`advisory_source == "package_fallback"`), the
advisory list is every advisory for the package, not the ones this PR closes.
Compare by hand:

```text
update.from = ">=7.3,<7.4"     advisory.vulnerableVersionRange = ">= 7.1, < 7.3.2"
                                advisory.firstPatchedVersion    = "7.3.2"
```

Here `from` may already be at or past `7.3.2`, so that advisory may not apply.
Read every range in `advisories[]` before endorsing the tier, and say in the
report which ones you confirmed.

## What is reported but deliberately not ranked

Folding these into the sort would make the order unexplainable, so they are
carried as fields for the model to raise in prose:

- **Runtime vs dev.** The starkest real case: `ol-keycloakify#174` is a **CVSS
  9.4** tier-0 PR for `@vitest/browser` — a browser-mode test runner. Nothing
  ships it to production. It outranks genuine runtime issues on score alone, so
  the report must say "devDependency, test-only blast radius" beside it. This is
  the highest-value correction the model makes to the machine ranking.
- **Direct vs transitive.** A transitive bump may not be reachable from your code
  at all.
- **Internet-facing vs internal.** Same CVE, very different exposure.
- **`blocked`** (`checks_failing` / `conflicting`). Does not change urgency, but
  changes what the user does next — 17 of 36 security PRs were blocked in the
  reference run.

## Grouped PRs

`grouped: true` means several packages in one PR. Severity is the **max** across
members, which can overstate the rest of the group. In the report, name the
member carrying the risk rather than implying every package in the PR is
critical.

## Unresolved advisory ids

`unresolved_ghsa_ids` lists ids Renovate cited that GitHub could not resolve —
seen on wagtail's `CVE-2026-54259`…`54263`. Causes: upstream-published advisories
not yet mirrored, or renamed/merged records. The vulnerability is real. Follow
the upstream advisory link in the PR body; do not treat the gap as absence of
risk.

## Ecosystems with no advisory coverage

Helm charts, `Dockerfile` `FROM` bumps and docker-compose images come back
`ecosystems: []` and always land in tier 5 or 6 regardless of what they fix — a
base-image bump closing a dozen OS CVEs is indistinguishable from a cosmetic one
in this data. Say so explicitly rather than implying the ranking covered them,
and point at the upstream image or chart release notes.
