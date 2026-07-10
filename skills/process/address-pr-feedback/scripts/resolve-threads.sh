#!/usr/bin/env bash
# Batch-resolve PR review threads from a JSON array on stdin:
#   [{"thread_id": "PRRT_...", "comment": "optional reply body"}, ...]
# Each thread is resolved independently -- one failure doesn't abort the rest.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dry_run=false
[[ "${1:-}" == "--dry-run" ]] && dry_run=true

input="$(cat)"
count="$(echo "$input" | jq 'length')"
ok=0
failed=0

for i in $(seq 0 $((count - 1))); do
  item="$(echo "$input" | jq ".[$i]")"
  thread_id="$(echo "$item" | jq -r '.thread_id')"
  comment="$(echo "$item" | jq -r '.comment // empty')"

  args=(--thread-id "$thread_id")
  [[ -n "$comment" ]] && args+=(--comment "$comment")
  [[ "$dry_run" == true ]] && args+=(--dry-run)

  if "${script_dir}/resolve-thread.sh" "${args[@]}"; then
    ok=$((ok + 1))
  else
    echo "FAILED: ${thread_id}" >&2
    failed=$((failed + 1))
  fi
done

echo "Resolved ${ok}/${count} thread(s)$([[ $failed -gt 0 ]] && echo ", ${failed} failed")" >&2
[[ "$failed" -eq 0 ]]
