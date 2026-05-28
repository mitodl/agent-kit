#!/usr/bin/env bash
# post-rfc-discussion.sh
# Creates a new RFC GitHub Discussion in mitodl/hq under the RFC category.
#
# Usage:
#   echo "<body>" | bash post-rfc-discussion.sh -t "RFC: My Title"
#   bash post-rfc-discussion.sh -t "RFC: My Title" -b "$(cat rfc.md)"
#   bash post-rfc-discussion.sh -t "RFC: My Title" -f /path/to/rfc.md
#
# Options:
#   -t TITLE   Discussion title (required; "RFC: " prefix added if absent)
#   -b BODY    RFC body text; reads from stdin if neither -b nor -f is given
#   -f FILE    Read body from FILE instead of -b / stdin
#
# Output: URL of the newly created discussion
# Requires: gh (authenticated), jq

set -euo pipefail

# ── Constants ─────────────────────────────────────────────────────────────────

REPO_ID="R_kgDOHOGzLg"
RFC_CATEGORY_ID="DIC_kwDOHOGzLs4COw0u"

# ── Argument parsing ──────────────────────────────────────────────────────────

TITLE=""
BODY=""
FILE=""

while getopts "t:b:f:" opt; do
	case "$opt" in
	t) TITLE="$OPTARG" ;;
	b) BODY="$OPTARG" ;;
	f) FILE="$OPTARG" ;;
	*)
		echo "Usage: $0 -t TITLE [-b BODY | -f FILE]" >&2
		exit 1
		;;
	esac
done

# ── Validation ────────────────────────────────────────────────────────────────

if [[ -z "$TITLE" ]]; then
	echo "Error: -t TITLE is required" >&2
	exit 1
fi

# Ensure title carries the RFC: prefix
if [[ "$TITLE" != RFC:* ]]; then
	TITLE="RFC: ${TITLE}"
fi

# Resolve body: -f FILE takes precedence over -b, then stdin
if [[ -n "$FILE" ]]; then
	if [[ ! -f "$FILE" ]]; then
		echo "Error: file not found: $FILE" >&2
		exit 1
	fi
	BODY="$(cat "$FILE")"
elif [[ -z "$BODY" ]]; then
	BODY="$(cat)"
fi

if [[ -z "$BODY" ]]; then
	echo "Error: RFC body is empty (pass -b, -f FILE, or pipe via stdin)" >&2
	exit 1
fi

# ── Post ──────────────────────────────────────────────────────────────────────

jq -n \
	--arg repoId "$REPO_ID" \
	--arg categoryId "$RFC_CATEGORY_ID" \
	--arg title "$TITLE" \
	--arg body "$BODY" \
	--arg query '
    mutation($repoId: ID!, $categoryId: ID!, $title: String!, $body: String!) {
      createDiscussion(input: {
        repositoryId: $repoId
        categoryId:   $categoryId
        title:        $title
        body:         $body
      }) {
        discussion { url number }
      }
    }' \
	'{
    query: $query,
    variables: {
      repoId:     $repoId,
      categoryId: $categoryId,
      title:      $title,
      body:       $body
    }
  }' |
	gh api graphql --input - |
	jq -r '.data.createDiscussion.discussion.url'
