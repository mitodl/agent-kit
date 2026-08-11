#!/usr/bin/env bash
# Resolve GitHub Advisory Database records for enriched Renovate PRs, and merge
# severity/CVSS/EPSS onto each PR record (jq/merge-advisories.jq).
#
# Also usable single-shot for ad-hoc checks:
#   advisory-lookup.sh --ghsa GHSA-xxxx-xxxx-xxxx
#   advisory-lookup.sh --package PIP cryptography
#
# READ-ONLY. Queries only -- this skill never mutates GitHub.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "${script_dir}/lib.sh"

usage() {
  cat >&2 <<'USAGE'
Usage: advisory-lookup.sh [enriched.json] [output.json]
       advisory-lookup.sh --ghsa GHSA-xxxx-xxxx-xxxx
       advisory-lookup.sh --package <ECOSYSTEM> <name>

Pipeline mode defaults to $enriched_json -> $scored_json (see paths.sh); prefer
the no-argument form, so the command string stays identical between runs.

Pipeline mode merges onto each PR: advisories[], max_severity, max_cvss,
max_epss_percentile, advisory_source, unresolved_ghsa_ids.

ECOSYSTEM is a SecurityAdvisoryEcosystem value: ACTIONS COMPOSER ERLANG GO
MAVEN NPM NUGET PIP PUB RUBYGEMS RUST SWIFT.
USAGE
  exit 1
}

# GraphQL fields fetched per advisory; jq/advisory.jq flattens them (and
# explains why CVSS has to be read from three places).
advisory_fields='
  ghsaId severity summary publishedAt withdrawnAt permalink
  cvss { score }
  cvssSeverities { cvssV3 { score } cvssV4 { score } }
  epss { percentage percentile }
  identifiers { type value }
  cwes(first: 4) { nodes { cweId } }
'

# `gh api graphql` exits non-zero when the response carries an `errors` array --
# and it prints the whole {data, errors} envelope to stdout, so a `--jq` filter
# never runs. A NOT_FOUND advisory would therefore write the error envelope into
# the results file, where it is non-empty (so it survives the empty-file sweep)
# but has no .ghsaId. Query without --jq, then validate before emitting anything.
lookup_ghsa() {
  local out
  out="$(gh api graphql -f query="{ securityAdvisory(ghsaId: \"$1\") { ${advisory_fields} } }" 2>/dev/null || true)"
  jq -e -L "$jq_dir" '
    include "advisory";
    (.data.securityAdvisory // empty) | normalize_advisory
  ' <<<"$out" 2>/dev/null || true
}

# `first: 100` is the API maximum. Ordering by UPDATED_AT DESC and taking fewer
# would silently drop *older* advisories, which is exactly the wrong end to cut:
# an old advisory can still cover the PR's current version, so truncating it
# understates max_severity. 100 covers every package observed in practice; when
# a package does exceed it, say so rather than ranking a partial set as whole.
lookup_package() {
  local out
  out="$(gh api graphql -f query="{ securityVulnerabilities(ecosystem: $1, package: \"$2\", first: 100, orderBy: {field: UPDATED_AT, direction: DESC}) {
           pageInfo { hasNextPage }
           nodes { vulnerableVersionRange firstPatchedVersion { identifier } advisory { ${advisory_fields} } } } }" 2>/dev/null || true)"
  if [[ "$(jq -r '.data.securityVulnerabilities.pageInfo.hasNextPage // false' <<<"$out" 2>/dev/null || echo false)" == "true" ]]; then
    echo "Warning: $1 package '$2' has more than 100 advisories; the fallback severity for it is based on a truncated set." >&2
  fi
  jq -L "$jq_dir" '
    include "advisory";
    [(.data.securityVulnerabilities.nodes // [])[]
     | (.advisory | normalize_advisory)
       + {vulnerableVersionRange, firstPatchedVersion: .firstPatchedVersion.identifier}]
  ' <<<"$out" 2>/dev/null || echo '[]'
}

# No "" case here: a bare invocation is the normal pipeline form and must fall
# through to the defaults below rather than printing usage.
case "${1:-}" in
  -h|--help) usage ;;
  --ghsa)
    [[ $# -ge 2 ]] || usage
    result="$(lookup_ghsa "$2")"
    if [[ -z "$result" ]]; then
      echo "No advisory found for $2 (not in the GitHub Advisory Database)" >&2
      echo 'null'
      exit 0
    fi
    echo "$result"
    exit 0 ;;
  --package)
    [[ $# -ge 3 ]] || usage
    lookup_package "$2" "$3"
    exit 0 ;;
esac

enriched="${1:-$enriched_json}"
output="${2:-$scored_json}"
[[ -f "$enriched" ]] || { echo "Error: no such file: ${enriched}" >&2; exit 1; }

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

# Stage 1: resolve every distinct GHSA id named in any PR body, once each.
#
# Work lists go through files rather than `mapfile` arrays: mapfile is bash 4.0+
# and macOS ships bash 3.2, so the array form would fail on a stock shell.
jq -r '[.[].ghsa_ids[]] | unique | .[]' "$enriched" > "${tmp_dir}/ghsa-ids.txt"
ghsa_total="$(grep -c . "${tmp_dir}/ghsa-ids.txt" || true)"
echo "Resolving ${ghsa_total} distinct advisory record(s)..." >&2

i=0
while IFS= read -r ghsa; do
  [[ -z "$ghsa" ]] && continue
  i=$((i + 1))
  lookup_ghsa "$ghsa" > "${tmp_dir}/adv-${i}.json" &
  wait_for_slot
done < "${tmp_dir}/ghsa-ids.txt"
wait || true
gather_json "$tmp_dir" adv "${tmp_dir}/advisories.json" '.'

resolved="$(jq 'length' "${tmp_dir}/advisories.json")"
if [[ "$resolved" -lt "$ghsa_total" ]]; then
  echo "Note: $(( ghsa_total - resolved )) of ${ghsa_total} GHSA id(s) cited by Renovate are not in GitHub's Advisory Database (upstream-only or renamed advisories); those PRs fall back to a package query." >&2
fi

# Stage 2: fall back to a package+ecosystem query for any security-relevant PR
# that stage 1 could not score. The trigger is "no advisory resolved", NOT "no
# GHSA id in the body" -- Renovate routinely cites advisory IDs that GitHub
# cannot resolve (e.g. wagtail's CVE-2026-54259..63), and keying off the body
# alone leaves those PRs silently unscored.
#
# This is less precise than a GHSA hit: it returns every advisory for the
# package, not only the ones this PR closes. Records are tagged
# advisory_source: "package_fallback" so the model knows to check
# vulnerableVersionRange against the "from" version before trusting the tier.
#
# Renovate's body table names the packages but not which manifest each came
# from, so there is no package -> ecosystem mapping to read. Querying only the
# PR's first ecosystem would ask the wrong database for the rest: a PR touching
# both package.json and pyproject.toml would look every Python package up as
# NPM. Instead, query every (ecosystem, package) pair the PR could plausibly
# mean -- a wrong pair simply returns no records -- and keep the ecosystem on
# each record so the merge can require it to match.
jq -r --argjson resolved "$(cat "${tmp_dir}/advisories.json")" '
  [$resolved[].ghsaId] as $ok
  | .[]
  | select(.security_marked or ((.ghsa_ids | length) > 0))
  | select([.ghsa_ids[] | select(. as $g | $ok | index($g))] | length == 0)
  | . as $pr
  | select(($pr.ecosystems | length) > 0)
  | $pr.ecosystems[] as $eco
  | $pr.updates[]?
  | [$eco, .package] | @tsv
' "$enriched" | sort -u > "${tmp_dir}/fallback-keys.tsv"

fallback='[]'
fallback_total="$(grep -c . "${tmp_dir}/fallback-keys.tsv" || true)"
if [[ "$fallback_total" -gt 0 ]]; then
  echo "Falling back to package lookups for ${fallback_total} unscored (ecosystem, package) pair(s)..." >&2
  j=0
  while IFS=$'\t' read -r eco pkg; do
    [[ -z "$eco" || -z "$pkg" ]] && continue
    j=$((j + 1))
    lookup_package "$eco" "$pkg" \
      | jq --arg pkg "$pkg" --arg eco "$eco" \
           'map(. + {package: $pkg, ecosystem: $eco})' > "${tmp_dir}/fb-${j}.json" &
    wait_for_slot
  done < "${tmp_dir}/fallback-keys.tsv"
  wait || true
  gather_json "$tmp_dir" fb "${tmp_dir}/fallback.json" 'add // []'
  fallback="$(cat "${tmp_dir}/fallback.json")"
fi

jq --argjson advisories "$(cat "${tmp_dir}/advisories.json")" \
   --argjson fallback "$fallback" \
   -f "${jq_dir}/merge-advisories.jq" "$enriched" > "$output"

scored="$(jq '[.[] | select(.max_severity != null)] | length' "$output")"
unscored="$(jq '[.[] | select((.security_marked or (.ghsa_ids | length > 0)) and .max_severity == null)] | length' "$output")"
echo "Scored ${scored} PR(s) from advisories; ${unscored} security-relevant PR(s) remain unscored -> $output" >&2
