#!/usr/bin/env bash
# Run pr-detail.sh over every PR in a fetch-prs.sh output file, in parallel,
# and merge the results (with `repo` attached) into one JSON array.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
max_parallel=8

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <fetched.json> <output.json>" >&2
  exit 1
fi

fetched="$1"
output="$2"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

i=0
while IFS=$'\t' read -r repo number; do
  i=$((i + 1))
  {
    "${script_dir}/pr-detail.sh" "$repo" "$number" 2>"${tmp_dir}/${i}.err" \
      | jq --arg repo "$repo" '. + {repo: $repo}' > "${tmp_dir}/${i}.json"
  } &
  while [[ "$(jobs -r -p | wc -l)" -ge "$max_parallel" ]]; do wait -n || true; done
done < <(jq -r '.[] | [.repository.nameWithOwner, .number] | @tsv' "$fetched")

wait || true

find "$tmp_dir" -name '*.json' -empty -delete
if compgen -G "${tmp_dir}/*.json" > /dev/null; then
  jq -s '.' "${tmp_dir}"/*.json > "$output"
else
  echo '[]' > "$output"
fi
echo "Enriched $(jq 'length' "$output") PR(s) -> $output" >&2
