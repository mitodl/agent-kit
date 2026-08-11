# Merge resolved advisory records onto each enriched PR.
#
# Run as: jq --argjson advisories <resolved GHSA records>
#            --argjson fallback   <package-query records>
#            -f merge-advisories.jq <enriched.json>
#
# Direct GHSA hits (from the PR body) win; the package fallback is only
# consulted when a PR has no direct hit, and its records are tagged
# advisory_source: "package_fallback" because they list every advisory for the
# package, not only the ones this PR closes.

def sev_rank: {"CRITICAL": 4, "HIGH": 3, "MODERATE": 2, "LOW": 1}[. // ""] // 0;

map(. as $pr
  | ( [$advisories[] | select(.ghsaId as $g | $pr.ghsa_ids | index($g))]
      | map(. + {advisory_source: "pr_body"}) ) as $direct
  | ( if ($direct | length) > 0 then []
      else [$fallback[] | select(.package as $p | ($pr.updates | map(.package) | index($p)))]
           | map(. + {advisory_source: "package_fallback"})
      end ) as $indirect
  | ($direct + $indirect | map(select(.withdrawn | not))) as $adv
  | $pr + {
      advisories: $adv,
      advisory_source: (if ($direct | length) > 0 then "pr_body"
                        elif ($indirect | length) > 0 then "package_fallback"
                        else null end),
      # GHSA ids Renovate cited that GitHub could not resolve. Worth surfacing:
      # the vulnerability is real, GitHub just has no record under that id.
      unresolved_ghsa_ids: [$pr.ghsa_ids[]
                            | select(. as $g | ([$advisories[].ghsaId] | index($g)) == null)],
      # A grouped PR inherits the max severity of its members.
      max_severity: ([$adv[].severity] | max_by(sev_rank) // null),
      max_cvss: ([$adv[].cvss | select(. != null)] | max // null),
      max_epss_percentile: ([$adv[].epss_percentile | select(. != null)] | max // null)
    })
