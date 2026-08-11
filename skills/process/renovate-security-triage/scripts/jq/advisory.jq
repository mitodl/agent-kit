# jq module shared by advisory-lookup.sh: normalize a GraphQL SecurityAdvisory
# node into the flat record the rest of the pipeline consumes.
# Used via: jq -L <this dir> 'include "advisory"; ...'

# GitHub reports CVSS in three places and leaves the others at 0.0: recent
# advisories score only cvssSeverities.cvssV4, older ones only the legacy
# `cvss`/cvssV3. Taking the max of all three (0 meaning "absent") is the only
# way to get a usable number. Verified against cryptography advisories:
# GHSA-jwv3-5hgf-82ww is cvss 0.0 / v3 0.0 / v4 8.7.
def advisory_score:
  [(.cvss.score // 0),
   (.cvssSeverities.cvssV3.score // 0),
   (.cvssSeverities.cvssV4.score // 0)] | max;

def normalize_advisory:
  {ghsaId, severity, summary, permalink, publishedAt,
   withdrawn: (.withdrawnAt != null),
   cvss: (advisory_score | if . == 0 then null else . end),
   epss_percentage: (.epss.percentage // null),
   epss_percentile: (.epss.percentile // null),
   cves: [.identifiers[] | select(.type == "CVE") | .value],
   cwes: [.cwes.nodes[].cweId]};
