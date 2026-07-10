#!/usr/bin/env bash
# Batch-resolve PR review threads from a JSON array on stdin:
#   [{"thread_id": "PRRT_...", "comment": "optional reply body"}, ...]
# Each thread is resolved independently -- one failure doesn't abort the rest.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dry_run=false
[[ "${1:-}" == "--dry-run" ]] && dry_run=true

input="$(cat)"
if [[ -z "$input" ]] || ! jq -e 'type == "array"' <<<"$input" >/dev/null 2>&1; then
  echo "Error: input must be a non-empty JSON array of {thread_id, comment} objects" >&2
  exit 1
fi

count="$(jq 'length' <<<"$input")"
ok=0
failed=0

while IFS= read -r item; do
  thread_id="$(jq -r '.thread_id' <<<"$item")"
  comment="$(jq -r '.comment // empty' <<<"$item")"

  args=(--thread-id "$thread_id")
  [[ -n "$comment" ]] && args+=(--comment "$comment")
  [[ "$dry_run" == true ]] && args+=(--dry-run)

  if "${script_dir}/resolve-thread.sh" "${args[@]}"; then
    ok=$((ok + 1))
  else
    echo "FAILED: ${thread_id}" >&2
    failed=$((failed + 1))
  fi
done < <(jq -c '.[]' <<<"$input")

echo "Resolved ${ok}/${count} thread(s)$([[ $failed -gt 0 ]] && echo ", ${failed} failed")" >&2
[[ "$failed" -eq 0 ]]
