#!/usr/bin/env bash
# Enumerate open PRs across a GitHub org (or user) authored by one user.
set -euo pipefail

usage() {
  echo "Usage: $0 <org> <output.json> [--author <user>]" >&2
  echo "  --author <user>   Filter to PRs authored by <user> (default: @me)" >&2
  exit 1
}

author="@me"
positional=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --author) [[ $# -ge 2 ]] || usage; author="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) positional+=("$1"); shift ;;
  esac
done

org="${positional[0]:-}"
output="${positional[1]:-}"
[[ -z "$org" || -z "$output" ]] && usage

gh search prs --owner "$org" --author "$author" --state open \
  --json number,title,url,repository,isDraft,createdAt,updatedAt,author \
  --limit 200 > "$output"

echo "Found $(jq 'length' "$output") open PR(s) authored by ${author} in ${org}" >&2
