# Assign an urgency tier to each advisory-enriched PR, sort by descending
# urgency, and wrap the result as {summary, prs}.
#
# Run as: jq -f classify.jq <scored.json>
# Tier semantics are documented in classify-renovate-prs.sh's header.

def tier_of:
  if .max_severity == "CRITICAL" then 0
  elif .max_severity == "HIGH"     then 1
  elif .max_severity == "MODERATE" then 2
  elif .max_severity == "LOW"      then 3
  # Security-relevant but unscored. Note this triggers on a body-cited GHSA id
  # too, not just the [SECURITY] title marker: Renovate omits that marker on
  # some real security fixes (verified on mitodl/ol-keycloakify#174, a CVSS 9.4
  # advisory titled only "chore(deps): update dependency @vitest/browser").
  elif (.security_marked or ((.ghsa_ids // []) | length > 0)) then 4
  elif .bump == "major"            then 5
  else                                  6 end;

def label_of:
  ["critical", "high", "moderate", "low",
   "security_unscored", "major_non_security", "routine"][.];

map(. as $pr
  | (tier_of) as $t
  | {
      tier: $t,
      urgency: ($t | label_of),
      repo: $pr.repo,
      number: $pr.number,
      title: $pr.title,
      url: $pr.url,
      age_days: (((now - ($pr.createdAt | fromdateiso8601)) / 86400) | floor),
      security_marked: $pr.security_marked,
      severity: $pr.max_severity,
      cvss: $pr.max_cvss,
      epss_percentile: $pr.max_epss_percentile,
      advisory_source: $pr.advisory_source,
      cve_ids: $pr.cve_ids,
      # Renovate cited these advisory ids but the GitHub database has no
      # record under them. The vulnerability is still real -- read the
      # upstream advisory rather than assuming the PR is unimportant.
      unresolved_ghsa_ids: ($pr.unresolved_ghsa_ids // []),
      # package_fallback records list every advisory for the package, not only
      # the ones this PR closes, so applicability is unproven. The model must
      # compare vulnerableVersionRange against the "from" version before
      # trusting the tier.
      needs_range_check: ($pr.advisory_source == "package_fallback"),
      advisories: [$pr.advisories[]
                   | {ghsaId, severity, cvss, summary, permalink,
                      cves, cwes,
                      vulnerableVersionRange: (.vulnerableVersionRange // null),
                      firstPatchedVersion: (.firstPatchedVersion // null)}],
      grouped: $pr.grouped,
      kind: $pr.kind,
      bump: $pr.bump,
      ecosystems: $pr.ecosystems,
      # Kept even though nothing here ranks on it: this file is the only
      # artifact the report phase reads, and the blast-radius judgment it is
      # asked to make (runtime vs dev, direct vs transitive) needs the changed
      # manifest paths as evidence.
      files: ($pr.files // []),
      updates: $pr.updates,
      # A security fix that cannot merge is worse than one that can, so
      # surface the blocker alongside the urgency rather than hiding it.
      blocked: (($pr.checks.failing > 0) or ($pr.mergeable == "CONFLICTING")),
      blocked_reason: (
        if   $pr.mergeable == "CONFLICTING" then "conflicting"
        elif $pr.checks.failing > 0         then "checks_failing"
        elif $pr.mergeable == "UNKNOWN"     then "mergeability_not_yet_computed"
        else null end),
      checks: $pr.checks,
      isDraft: $pr.isDraft,
      createdAt: $pr.createdAt,
      updatedAt: $pr.updatedAt
    })

| sort_by([.tier,
           -(.cvss // -1),
           -(.epss_percentile // -1),
           .createdAt])

| { summary: {
      total: length,
      by_urgency: (group_by(.urgency)
                   | map({key: .[0].urgency, value: length}) | from_entries),
      actionable_security: ([.[] | select(.tier <= 4)] | length),
      blocked_security: ([.[] | select(.tier <= 4 and .blocked)] | length),
      repos: ([.[].repo] | unique | length)
    },
    prs: . }
