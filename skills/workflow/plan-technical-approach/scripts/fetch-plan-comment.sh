#!/usr/bin/env bash
# Find this skill's plan comment on a GitHub issue. Read-only: it never writes.
#
# Exists as its own script so the agent can ask "is there a plan comment, and
# what does it say?" without going anywhere near a mutation -- and so
# upsert-plan-comment.sh has exactly one implementation of the lookup to share.
#
# Usage:
#   fetch-plan-comment.sh --repo <owner/repo> --issue <N|url> [--json]
#
# Options:
#   --repo <owner/repo>  Required. Never inferred from the working directory:
#                        under the mitodl/hq tracker split the issue lives in a
#                        different repo from the code, and a cwd-derived repo is
#                        how a plan lands on the wrong issue.
#   --issue <N|url>      Issue number, or a full GitHub issue URL (which also
#                        supplies the repo, so --repo may be omitted with one).
#   --json               Emit the raw match array instead of the summary.
#
# Output: on stdout, one summary block per matching comment (id, node_id,
#   author, sha, url) followed by the body; nothing at all when there is no
#   match. Diagnostics go to stderr. Exit 0 whether or not a match was found --
#   "no plan comment yet" is a normal answer, not an error.
#
# Requires: gh (GitHub CLI, authenticated), jq
set -euo pipefail

# shellcheck source=paths.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/paths.sh"

usage() {
  echo "Usage: $0 --repo <owner/repo> --issue <N|url> [--json]" >&2
  exit 1
}

repo=""
issue=""
as_json=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)  repo="${2:?--repo needs <owner/repo>}"; shift 2 ;;
    --issue) issue="${2:?--issue needs a number or URL}"; shift 2 ;;
    --json)  as_json=true; shift ;;
    -h|--help) usage ;;
    *) echo "Error: unknown argument: $1" >&2; usage ;;
  esac
done

# A full issue URL carries the repo too, so accept it in place of both flags.
if [[ "$issue" =~ ^https?://[^/]+/([^/]+/[^/]+)/issues/([0-9]+) ]]; then
  repo="${repo:-${BASH_REMATCH[1]}}"
  issue="${BASH_REMATCH[2]}"
fi

[[ -n "$repo"  ]] || { echo "Error: --repo is required (or pass a full issue URL to --issue)" >&2; usage; }
[[ -n "$issue" ]] || { echo "Error: --issue is required" >&2; usage; }
[[ "$issue" =~ ^[0-9]+$ ]] || { echo "Error: --issue must be a number or an issue URL, got: ${issue}" >&2; exit 1; }

all="$(mktemp)"
trap 'rm -f "$all"' EXIT

echo "Fetching comments on ${repo}#${issue}..." >&2

# per_page goes in the query string, NOT as -F: `gh api` switches the method to
# POST as soon as any request parameter is added, which on this endpoint would
# CREATE a comment instead of listing them. --slurp wraps the pages in an outer
# array, hence the .[][] flatten below.
gh api --paginate --slurp \
  "repos/${repo}/issues/${issue}/comments?per_page=100" > "$all"

# Filtered in a separate jq because `gh --jq` offers no --arg, and the marker
# must not be interpolated into the program text.
matches="$(jq -c --arg m "$plan_marker" \
  '[ .[][]
     | {id, node_id, author: .user.login, url: .html_url, body}
     | select(.body | startswith($m)) ]' "$all")"

count="$(jq 'length' <<<"$matches")"
echo "Found ${count} plan comment(s)." >&2

if [[ "$as_json" == true ]]; then
  jq '.' <<<"$matches"
  exit 0
fi

jq -r --arg p "$sha_marker_prefix" '
  .[] |
  "id: \(.id)",
  "node_id: \(.node_id)",
  "author: \(.author)",
  "url: \(.url)",
  "sha: \(
     [ (.body | split("\n")[]) | select(startswith($p)) ]
     | if length == 0 then "(none)"
       else (.[-1] | ltrimstr($p) | rtrimstr(" -->") | rtrimstr("-->"))
       end
   )",
  "---",
  .body
' <<<"$matches"
