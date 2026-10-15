#!/usr/bin/env bash
# Create or update the plan comment on a GitHub issue.
#
# The plan lives in ONE comment that is edited in place as the work evolves,
# rather than a new comment per revision, so the issue timeline stays readable
# on long-running work. The comment is identified by a marker on its first
# line; this script owns both halves of that contract -- it prepends the marker
# so a caller cannot forget it, and it is the only thing that looks for it.
#
# Deliberately NOT `gh issue comment --edit-last`: that edits the current
# user's most recent comment on the issue, whatever that comment happened to
# be, which silently overwrites an unrelated remark. And not `gh search
# issues`: search does not index HTML comments.
#
# Usage:
#   upsert-plan-comment.sh --repo <owner/repo> --issue <N|url> [options]
#
# Options:
#   --repo <owner/repo>    Required. Never inferred from the working directory:
#                          under the mitodl/hq tracker split the issue lives in
#                          a different repo from the code.
#   --issue <N|url>        Issue number, or a full issue URL (which supplies
#                          the repo too).
#   --body-file <file|->   Plan body. `-` reads stdin. Defaults to this issue's
#                          draft path under ~/.cache/ol-plan/.
#                          Prefer the file over any inline form: a body passed
#                          as a shell argument has its backticks and $ expanded
#                          before this script ever sees it, and plan text is
#                          full of backticked identifiers.
#   --expect-sha <sha256>  Refuse to write unless the live comment carries this
#                          digest. Guards against clobbering another session's
#                          edit; GitHub's PATCH ignores If-Match, so the check
#                          has to travel in the body.
#   --on-duplicate <mode>  fail (default) | oldest | newest. With oldest/newest
#                          the chosen comment is edited and the losers are
#                          minimized as OUTDATED.
#   --require-existing     Fail instead of creating when no marker is found.
#                          Pass this on every update after the first, so a
#                          transient read failure cannot open a second comment.
#   --dry-run              Report what would happen and write nothing. Still
#                          performs the (read-only) lookup, because
#                          create-vs-update is the whole question.
#   --print-draft-path     Print the draft path for this issue and exit. The
#                          path is derived, not fixed, so this is how a caller
#                          learns where to write the body.
#
# Output: the comment URL on stdout; diagnostics on stderr.
# Requires: gh (GitHub CLI, authenticated), jq, sha256sum (or shasum)
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=paths.sh
source "${script_dir}/paths.sh"

usage() {
  echo "Usage: $0 --repo <owner/repo> --issue <N|url> [--body-file <file|->]" >&2
  echo "          [--expect-sha <sha256>] [--on-duplicate fail|oldest|newest]" >&2
  echo "          [--require-existing] [--dry-run]" >&2
  echo "Prefer --body-file over any inline body: a shell argument has its" >&2
  echo "backticks and \$ expanded before this script sees it." >&2
  exit 1
}

repo=""
issue=""
body_file=""
expect_sha=""
on_duplicate="fail"
require_existing=false
dry_run=false
print_path=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)             repo="${2:?--repo needs <owner/repo>}"; shift 2 ;;
    --issue)            issue="${2:?--issue needs a number or URL}"; shift 2 ;;
    --body-file)        body_file="${2:?--body-file needs a path (use - for stdin)}"; shift 2 ;;
    --expect-sha)       expect_sha="${2:?--expect-sha needs a digest}"; shift 2 ;;
    --on-duplicate)     on_duplicate="${2:?--on-duplicate needs fail|oldest|newest}"; shift 2 ;;
    --require-existing) require_existing=true; shift ;;
    --dry-run)          dry_run=true; shift ;;
    --print-draft-path) print_path=true; shift ;;
    -h|--help)          usage ;;
    *) echo "Error: unknown argument: $1" >&2; usage ;;
  esac
done

if [[ "$issue" =~ ^https?://[^/]+/([^/]+/[^/]+)/issues/([0-9]+) ]]; then
  repo="${repo:-${BASH_REMATCH[1]}}"
  issue="${BASH_REMATCH[2]}"
fi

[[ -n "$repo"  ]] || { echo "Error: --repo is required (or pass a full issue URL to --issue)" >&2; usage; }
[[ -n "$issue" ]] || { echo "Error: --issue is required" >&2; usage; }
[[ "$issue" =~ ^[0-9]+$ ]] || { echo "Error: --issue must be a number or an issue URL, got: ${issue}" >&2; exit 1; }
case "$on_duplicate" in
  fail|oldest|newest) ;;
  *) echo "Error: --on-duplicate must be fail, oldest or newest, got: ${on_duplicate}" >&2; exit 1 ;;
esac

# ---- resolve the body -------------------------------------------------------

[[ -n "$body_file" ]] || body_file="$(draft_path "$repo" "$issue")"

if [[ "$print_path" == true ]]; then
  printf '%s\n' "$body_file"
  exit 0
fi

raw="$(mktemp)"; payload="$(mktemp)"; all="$(mktemp)"
trap 'rm -f "$raw" "$payload" "$all"' EXIT

if [[ "$body_file" == "-" ]]; then
  cat > "$raw"
elif [[ -r "$body_file" ]]; then
  cat "$body_file" > "$raw"
else
  echo "Error: cannot read body file: ${body_file}" >&2
  echo "Hint: the agent writes the draft there first; pass --body-file to override." >&2
  exit 1
fi

[[ -s "$raw" ]] || { echo "Error: plan body is empty: ${body_file}" >&2; exit 1; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1
  else shasum -a 256 | cut -d' ' -f1
  fi
}

# Strip any markers the caller included so re-running on an already-marked body
# cannot double them, then rebuild: marker, body, digest over everything above.
grep -v -F -e "$plan_marker" -e "$sha_marker_prefix" "$raw" > "$payload.stripped" || true
{
  printf '%s\n\n' "$plan_marker"
  cat "$payload.stripped"
} > "$payload.unsigned"
new_sha="$(sha256_of < "$payload.unsigned")"
{
  cat "$payload.unsigned"
  printf '\n%s%s -->\n' "$sha_marker_prefix" "$new_sha"
} > "$payload"
rm -f "$payload.stripped" "$payload.unsigned"

# GitHub rejects an issue comment body over 65536 characters. Catch it here so
# the failure names the number instead of surfacing as an opaque 422.
size="$(wc -c < "$payload" | tr -d ' ')"
if (( size > 65536 )); then
  echo "Error: plan body is ${size} bytes; GitHub's limit is 65536." >&2
  echo "Trim the Revisions log to the most recent entries plus an elided count." >&2
  exit 6
fi

# ---- preflight --------------------------------------------------------------

me="$(gh api user --jq '.login')"
# Triage is enough to comment on GitHub, so accept it rather than refusing a
# user who can legitimately do this. (The message used to name triage while
# the check required push, telling people the refusal was about a permission
# they had.)
perms="$(gh api "repos/${repo}" --jq '[.permissions.push, .permissions.triage] | @tsv' 2>/dev/null || printf 'false\tfalse')"
can_push="$(cut -f1 <<<"$perms")"
can_triage="$(cut -f2 <<<"$perms")"
if [[ "$can_push" != "true" && "$can_triage" != "true" ]]; then
  echo "Error: ${me} has neither push nor triage on ${repo}." >&2
  echo "Commenting on an issue there needs one of them." >&2
  exit 3
fi

# ---- find the existing comment ---------------------------------------------

echo "Looking for an existing plan comment on ${repo}#${issue}..." >&2

# per_page in the query string, NOT as -F: any request parameter flips gh api
# to POST, which on this endpoint would create a comment instead of listing.
gh api --paginate --slurp \
  "repos/${repo}/issues/${issue}/comments?per_page=100" > "$all"

matches="$(jq -c --arg m "$plan_marker" \
  '[ .[][]
     | {id, node_id, author: .user.login, url: .html_url, body}
     | select(.body | startswith($m)) ]' "$all")"
count="$(jq 'length' <<<"$matches")"

target=""
losers="[]"
if (( count == 1 )); then
  target="$(jq -c '.[0]' <<<"$matches")"
elif (( count > 1 )); then
  case "$on_duplicate" in
    fail)
      echo "Error: ${count} plan comments on ${repo}#${issue}; refusing to guess." >&2
      jq -r '.[] | "  id=\(.id) author=\(.author) \(.url)"' <<<"$matches" >&2
      echo "Re-run with --on-duplicate oldest|newest to keep one and minimize the rest." >&2
      exit 2 ;;
    oldest) target="$(jq -c '.[0]'  <<<"$matches")"; losers="$(jq -c '.[1:]'   <<<"$matches")" ;;
    newest) target="$(jq -c '.[-1]' <<<"$matches")"; losers="$(jq -c '.[:-1]'  <<<"$matches")" ;;
  esac
fi

if [[ -z "$target" && "$require_existing" == true ]]; then
  echo "Error: no plan comment found on ${repo}#${issue} and --require-existing was passed." >&2
  echo "Refusing to create a second one. Check whether the first was deleted or minimized." >&2
  exit 5
fi

# ---- concurrency guard ------------------------------------------------------

if [[ -n "$expect_sha" && -n "$target" ]]; then
  live_sha="$(jq -r --arg p "$sha_marker_prefix" '
    [ (.body | split("\n")[]) | select(startswith($p)) ]
    | if length == 0 then "" else (.[-1] | ltrimstr($p) | rtrimstr(" -->") | rtrimstr("-->")) end
  ' <<<"$target")"
  if [[ "$live_sha" != "$expect_sha" ]]; then
    echo "Error: plan comment changed since it was read." >&2
    echo "  expected: ${expect_sha}" >&2
    echo "  live:     ${live_sha:-(no digest)}" >&2
    echo "  comment:  $(jq -r '.url' <<<"$target")" >&2
    echo "Re-read it with fetch-plan-comment.sh and merge before writing again." >&2
    exit 4
  fi
fi

if [[ -n "$target" ]]; then
  target_author="$(jq -r '.author' <<<"$target")"
  if [[ "$target_author" != "$me" ]]; then
    echo "Warning: comment was authored by ${target_author}; the edit will show as 'edited by ${me}'." >&2
  fi
fi

# ---- act --------------------------------------------------------------------

if [[ "$dry_run" == true ]]; then
  if [[ -n "$target" ]]; then
    echo "DRY-RUN: would PATCH repos/${repo}/issues/comments/$(jq -r '.id' <<<"$target")" >&2
    echo "         matches=${count} bytes=${size} sha=${new_sha} author=$(jq -r '.author' <<<"$target")" >&2
    if [[ "$(jq 'length' <<<"$losers")" -gt 0 ]]; then
      jq -r '.[] | "         would minimize \(.url)"' <<<"$losers" >&2
    fi
  else
    echo "DRY-RUN: would POST a new comment to repos/${repo}/issues/${issue}" >&2
    echo "         matches=0 bytes=${size} sha=${new_sha}" >&2
  fi
  cat "$payload"
  exit 0
fi

post_new() {
  jq -n --rawfile body "$payload" '{body: $body}' \
    | gh api --method POST "repos/${repo}/issues/${issue}/comments" \
        --input - --jq '.html_url'
}

if [[ -n "$target" ]]; then
  comment_id="$(jq -r '.id' <<<"$target")"
  patch_err="$(mktemp)"
  if url="$(jq -n --rawfile body "$payload" '{body: $body}' \
      | gh api --method PATCH "repos/${repo}/issues/comments/${comment_id}" \
          --input - --jq '.html_url' 2>"$patch_err")"; then
    echo "Updated plan comment on ${repo}#${issue}." >&2
    rm -f "$patch_err"
  elif grep -q 'HTTP 404' "$patch_err"; then
    # ONLY a 404 means the comment was deleted between the read and the write,
    # and only then is creating a replacement right. Recreating on any failure
    # turns a 403, a 422, a 502 or a rate limit into a SECOND plan comment --
    # manufacturing the duplicate this marker design exists to prevent, and
    # wedging every later run against --on-duplicate fail.
    echo "Warning: comment ${comment_id} is gone (404); posting a new one." >&2
    rm -f "$patch_err"
    url="$(post_new)"
  else
    echo "Error: failed to update comment ${comment_id} on ${repo}#${issue}." >&2
    sed 's/^/  /' "$patch_err" >&2
    echo "Not posting a replacement: the comment still exists, and a second" >&2
    echo "one would split the plan record. Re-run once the cause is cleared." >&2
    rm -f "$patch_err"
    exit 7
  fi

  # Minimize rather than delete: deleting destroys the Revisions log the
  # superseded comment carries, and minimized comments stay readable.
  while read -r node_id loser_url; do
    [[ -n "$node_id" ]] || continue
    # shellcheck disable=SC2016  # single-quoted on purpose: $id is a GraphQL variable
    if gh api graphql -f query='
         mutation($id: ID!) {
           minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) {
             minimizedComment { isMinimized }
           }
         }' -f id="$node_id" >/dev/null 2>&1; then
      echo "Minimized duplicate ${loser_url}" >&2
    else
      echo "Warning: could not minimize ${loser_url}" >&2
    fi
  done < <(jq -r '.[] | "\(.node_id) \(.url)"' <<<"$losers")
else
  url="$(post_new)"
  echo "Created plan comment on ${repo}#${issue}." >&2
fi

echo "$url"
