#!/usr/bin/env bash
# Assign an urgency tier to each advisory-enriched Renovate PR and sort by
# descending urgency (jq/classify.jq). Writes a file (NOT stdout -- a
# caller-supplied `>` makes this a compound command, which permission
# allowlists match poorly).
#
# Tiers (0 is most urgent):
#   0  CRITICAL advisory
#   1  HIGH advisory
#   2  MODERATE advisory
#   3  LOW advisory
#   4  Renovate marked [SECURITY] but no advisory record resolved -- unscored,
#      needs a human/model look; never silently demoted below plain updates
#   5  non-security major bump (breaking-change risk, not security risk)
#   6  non-security minor / patch / digest / pin / lockfile-only
#
# Within a tier: CVSS desc, then EPSS percentile desc, then oldest PR first.
#
# READ-ONLY. Reads a file, writes a file. Nothing here touches GitHub.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "${script_dir}/lib.sh"

case "${1:-}" in
  -h|--help)
    echo "Usage: $0 [advisory-enriched.json] [output.json]" >&2
    echo "  Defaults to \$scored_json -> \$classified_json (see paths.sh); prefer" >&2
    echo "  the no-argument form, so the command string stays identical between runs." >&2
    echo "  Emits {summary, prs}." >&2
    exit 1 ;;
esac

input="${1:-$scored_json}"
output="${2:-$classified_json}"
[[ -f "$input" ]] || { echo "Error: no such file: $input" >&2; exit 1; }

jq -f "${jq_dir}/classify.jq" "$input" > "$output"

# Printed here so the caller never has to run a separate `jq .summary` -- one
# fewer command to approve, and the counts are what the report leads with.
jq -c '.summary' "$output" >&2
echo "Classified $(jq '.summary.total' "$output") PR(s); $(jq '.summary.actionable_security' "$output") security-relevant -> $output" >&2
