# shellcheck shell=bash
# Shared file locations for the plan-comment scripts.
#
# Sourced by the scripts here, and by implement-plan, which updates the same
# comment at every phase and chunk boundary. The cache directory is named for
# the artifact rather than for either skill, so renaming a skill does not
# orphan in-flight drafts.
#
# Why this exists: the agent writes the plan draft to a file and the upsert
# script reads it back, so if the *caller* has to name that file then the bash
# command string changes between runs -- which means a permission-allowlist
# entry approved once never matches again. (Agent harnesses that rewrite /tmp
# into a session-scoped scratchpad directory are the usual culprit: the session
# UUID is new every time.) Deriving the path here keeps the documented
# invocation byte-identical across runs for a given issue.
#
# The path is issue-scoped rather than a single fixed file because two agents
# planning two different issues on one machine would otherwise publish over
# each other. Two agents on the SAME issue is a real conflict, and that is what
# upsert-plan-comment.sh's --expect-sha guard is for, not this.
#
# OL_PLAN_DIR relocates the whole set without changing any command.

plan_dir="${OL_PLAN_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/ol-plan}"
mkdir -p "$plan_dir"
# Drafts quote private repo names, unreleased design detail and issue bodies.
# The caller's umask is commonly 022, which would leave those world-readable on
# a shared machine, so pin the directory to owner-only.
chmod 700 "$plan_dir" 2>/dev/null || true

# Derive the draft path for one issue. Call as: draft_path <owner/repo> <number>
# Slashes in the repo become dashes so the result stays a single flat filename.
draft_path() {
  local repo="$1" issue="$2"
  printf '%s/%s-%s.md\n' "$plan_dir" "${repo//\//-}" "$issue"
}

# The marker that identifies this skill's comment, and the prefix of the
# trailing digest line. Both are matched literally, so they are defined once
# here rather than repeated in each script.
# shellcheck disable=SC2034  # consumed by the sourcing script
plan_marker='<!-- ol-plan:v1 -->'
# shellcheck disable=SC2034
sha_marker_prefix='<!-- ol-plan:sha:'

# The marker a RETIRED plan comment carries. When --on-duplicate oldest|newest
# keeps one comment and minimizes the rest, the losers also get this marker in
# place of the live one. Minimizing alone is not enough: a minimized comment
# keeps its body and its marker in the issue-comments API, which both lookups
# read, so the duplicate stayed discoverable and the next ordinary upsert failed
# on the same "2 plan comments" error the recovery was supposed to clear.
# Deliberately NOT a prefix of plan_marker, so `startswith($plan_marker)` in
# both lookups stops selecting it.
# shellcheck disable=SC2034  # consumed by the sourcing script
retired_marker='<!-- ol-plan:v1:retired -->'

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1
  else shasum -a 256 | cut -d' ' -f1
  fi
}

# A locale under which `wc -m` really counts CHARACTERS, or "C" if there is
# none. Both ceilings this tooling enforces -- the 280-character note and
# GitHub's 65536-character comment body -- are counts of characters, and this
# house style is full of em dashes and arrows.
#
# `C.UTF-8` cannot just be assumed: it is a glibc locale. On macOS it does not
# exist, the setting falls back to C, and `wc -m` silently counts BYTES --
# reintroducing the exact bug the pinned locale was added to prevent, and
# rejecting a compliant body as oversized.
#
# Tested by measuring rather than by asking `locale`: an em dash is three bytes
# and one character, so a locale that reports 1 is one that works. That needs
# no extra binary and tests the thing actually being relied on.
utf8_locale() {
  local l
  for l in "${LC_ALL:-}" "${LC_CTYPE:-}" C.UTF-8 en_US.UTF-8 UTF-8; do
    [ -n "$l" ] || continue
    if [ "$(LC_ALL="$l" printf '\xe2\x80\x94' | LC_ALL="$l" wc -m | tr -d ' ')" = "1" ]; then
      printf '%s' "$l"
      return 0
    fi
  done
  printf 'C'
  return 1
}

# Resolved once per process; every call site uses it rather than pinning a
# locale of its own. Empty when nothing worked, which callers report rather
# than passing off a byte count as a character count.
if plan_utf8_locale="$(utf8_locale)"; then
  plan_utf8_ok=true
else
  plan_utf8_ok=false
fi
# shellcheck disable=SC2034  # both are consumed by the sourcing script
: "$plan_utf8_locale" "$plan_utf8_ok"

# Digest of a plan comment, read from stdin, for --expect-sha.
#
# It covers the plan TEXT, not the assembled comment: every marker line is
# removed, CRs are stripped, and leading and trailing blank lines are trimmed
# before hashing. That canonical form is what lets BOTH sides compute the same
# number -- the writer from the draft it is about to post, the reader from the
# body it just fetched.
#
# The earlier definition hashed the assembled payload instead, which only the
# writer could reproduce. So the guard had to read the digest the comment
# reported about itself, out of its own footer, and a GitHub UI edit leaves that
# footer untouched: a session holding the pre-edit sha passed the check and
# overwrote the edit. Hashing the text closes that, because an edited body
# hashes differently however the footer reads.
#
# Interior blank lines are preserved -- they are part of the markdown.
plan_digest() {
  sed 's/\r$//' \
    | grep -v -F -e "$plan_marker" -e "$retired_marker" -e "$sha_marker_prefix" \
    | awk '
        /^[[:space:]]*$/ { if (seen) { pending++ } ; next }
        { while (pending-- > 0) print ""; pending = 0; seen = 1; print }
      ' \
    | sha256_of
}
