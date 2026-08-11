#!/usr/bin/env bash
# Fetch per-PR detail for every PR in a fetch-renovate-prs.sh output file (in
# parallel), then parse Renovate's PR body into structured update records.
# The parsing itself lives in jq/enrich.jq.
#
# READ-ONLY. Queries only -- this skill never mutates GitHub.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "${script_dir}/lib.sh"

usage() {
  echo "Usage: $0 [fetched.json] [output.json]" >&2
  echo "  Defaults to \$renovate_json -> \$enriched_json (see paths.sh); prefer" >&2
  echo "  the no-argument form, so the command string stays identical between runs." >&2
  echo "  Emits the input records plus: body, labels, files, ghsa_ids," >&2
  echo "  cve_ids, ecosystems, updates[], bump, checks, mergeable" >&2
  exit 1
}

case "${1:-}" in -h|--help) usage ;; esac
fetched="${1:-$renovate_json}"
output="${2:-$enriched_json}"
[[ -f "$fetched" ]] || { echo "Error: no such file: ${fetched}" >&2; exit 1; }

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

total="$(jq 'length' "$fetched")"
echo "Enriching ${total} PR(s) (${max_parallel}-way parallel)..." >&2

# One detail file per PR; a failed fetch leaves an empty file, which
# gather_json sweeps away before combining.
i=0
while IFS=$'\t' read -r repo number; do
  i=$((i + 1))
  gh pr view "$number" -R "$repo" \
    --json number,body,labels,files,statusCheckRollup,mergeable,mergeStateStatus \
    2>/dev/null \
    | jq --arg repo "$repo" '. + {repo: $repo}' \
    > "${tmp_dir}/detail-${i}.json" &
  wait_for_slot
done < <(jq -r '.[] | [.repo, .number] | @tsv' "$fetched")
wait || true

gather_json "$tmp_dir" detail "${tmp_dir}/details.json" '.'

detail_count="$(jq 'length' "${tmp_dir}/details.json")"
if [[ "$detail_count" -lt "$total" ]]; then
  echo "Warning: ${total} PR(s) requested but only ${detail_count} detail fetch(es) succeeded (deleted branch or permissions)." >&2
fi

jq -s -f "${jq_dir}/enrich.jq" "$fetched" "${tmp_dir}/details.json" > "$output"

echo "Enriched $(jq 'length' "$output") PR(s); $(jq '[.[] | select(.ghsa_ids | length > 0)] | length' "$output") carry advisory IDs -> $output" >&2
