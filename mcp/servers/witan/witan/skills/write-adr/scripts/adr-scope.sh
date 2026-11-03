#!/usr/bin/env bash
# Which ADRs govern a change, and whether their `applies_to` globs still work.
#
# Four checks used to sit inlined across three skills -- implement-plan Step 1
# (match the diff, list the unscoped), Step 6.3 (re-check a forward-looking
# note) and complete-project Step 3 (the same re-check, as a verdict). They had
# already drifted apart, and all of it was shell an agent retyped by hand.
#
# Two things this gets right that the obvious version does not:
#
# 1. `:(glob)` pathspec semantics are applied HERE, so a caller cannot forget
#    the prefix. Without it `*` crosses `/`: `git ls-files -- 'mcp/*.py'`
#    returns 211 files where `:(glob)mcp/*.py` returns 0. A narrow-looking glob
#    silently matching a whole subtree is the failure the prose keeps warning
#    about, and the warning does not work.
#
# 2. The UNSCOPED list is printed by every mode, not as a footnote. `applies_to`
#    is new: 0 of witan's 11 ADRs carry it today, so "no ADR governs this
#    change" is routinely what a correct run prints while four inherited
#    decisions bind just as hard. Zero governing ADRs is a conclusion only
#    after a human has read the unscoped ones.
#
# Usage:
#   adr-scope.sh [--dir <adr-dir>] --changed <base-ref>
#   adr-scope.sh [--dir <adr-dir>] --paths "<glob> [<glob>...]"
#   adr-scope.sh [--dir <adr-dir>] --sweep
#
# Modes:
#   --changed <ref>  Which ADRs govern the diff <ref>...HEAD. Run before
#                    editing, and again before opening the pull request.
#   --paths <globs>  Which ADRs govern a chunk's declared `Touches` paths,
#                    BEFORE its first edit. An ADR found after the code is
#                    written is a rewrite; found before, it is a constraint.
#   --sweep          Re-check every forward-looking note now the work has
#                    merged. Only here is an empty match a verdict.
#
# Options:
#   --dir <path>     The ADR directory (default: docs/adr).
#   --quiet          Suppress the unscoped list. For a second run in the same
#                    session, never for the first.
#
# Exit: 0 clean; 1 usage or cannot run; 2 a glob matches nothing and nothing
#       says it should; 3 (--sweep only) a forward-looking note is now proven
#       wrong -- the work is over and it governs nothing.
set -euo pipefail

dir="docs/adr"
mode=""
base=""
paths=""
quiet=false

usage() {
  sed -n '/^# Usage:/,/^#       wrong/p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)     dir="${2:?--dir needs a path}"; shift 2 ;;
    --changed) mode="changed"; base="${2:?--changed needs a base ref}"; shift 2 ;;
    --paths)   mode="paths";  paths="${2:?--paths needs one or more globs}"; shift 2 ;;
    --sweep)   mode="sweep"; shift ;;
    --quiet)   quiet=true; shift ;;
    -h|--help) usage ;;
    *) echo "Error: unknown argument: $1" >&2; usage ;;
  esac
done

[[ -n "$mode" ]] || { echo "Error: one of --changed, --paths or --sweep is required." >&2; usage; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "Error: not inside a git work tree; every check here is a pathspec." >&2; exit 1; }
[[ -d "$dir" ]] || { echo "Error: no such ADR directory: ${dir}" >&2; exit 1; }

if [[ "$mode" == "changed" ]]; then
  git rev-parse --verify --quiet "$base" >/dev/null \
    || { echo "Error: cannot resolve base ref: ${base}" >&2; exit 1; }
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# ---- parsing ----------------------------------------------------------------

# The `applies_to` globs of one ADR, one per line, or nothing.
#
# Read only inside the FRONTMATTER. A `applies_to:` written in the prose of an
# ADR *about* this convention -- which is exactly the kind of ADR this repo
# writes -- would otherwise be parsed as metadata.
applies_to_of() {
  awk '
    NR == 1 && $0 == "---" { fm = 1; next }
    fm && $0 == "---"      { exit }
    !fm                    { exit }
    /^applies_to:[[:space:]]*$/ { inlist = 1; next }
    inlist && /^[[:space:]]+-[[:space:]]*/ {
      sub(/^[[:space:]]+-[[:space:]]*/, "")
      gsub(/^["'"'"']|["'"'"'][[:space:]]*$/, "")
      sub(/[[:space:]]+$/, "")
      if (length($0)) print
      next
    }
    inlist { inlist = 0 }
  ' "$1"
}

# Does this ADR declare <glob> as a path the work creates?
#   <!-- applies_to: src/notifications/** is created by this work -->
declared_forward() {
  grep -qF -- "<!-- applies_to: $2 is created by this work -->" "$1"
}

# Every tracked file an entry matches, with :(glob) applied here so no caller
# can forget it.
files_matching() {
  git ls-files -- ":(glob)$1" 2>/dev/null || true
}

# ---- the sets a mode compares against ---------------------------------------

case "$mode" in
  changed)
    git diff --name-only "$base"...HEAD > "$work/target" || {
      echo "Error: could not diff ${base}...HEAD" >&2; exit 1; }
    ;;
  paths)
    : > "$work/target"
    for g in $paths; do
      m="$(files_matching "$g")"
      if [[ -z "$m" ]]; then
        echo "Note: Touches glob matches no tracked file: ${g}" >&2
        echo "      Either this chunk creates it, or the glob is wrong. An ADR" >&2
        echo "      scoped only to it cannot be matched here -- judge by hand." >&2
      else
        printf '%s\n' "$m" >> "$work/target"
      fi
    done
    sort -u -o "$work/target" "$work/target"
    ;;
esac

# ---- walk the ADRs ----------------------------------------------------------

status=0
governing=0
unscoped=()
scoped=0

shopt -s nullglob
for adr in "$dir"/[0-9]*.md; do
  base_name="${adr##*/}"
  mapfile -t entries < <(applies_to_of "$adr")

  if [[ "${#entries[@]}" -eq 0 ]]; then
    unscoped+=("$base_name")
    continue
  fi
  scoped=$(( scoped + 1 ))

  hit=false
  for entry in "${entries[@]}"; do
    tracked="$(files_matching "$entry")"

    # An entry matching nothing in the tree. Two causes, and only the mode
    # knows which is a verdict.
    if [[ -z "$tracked" ]]; then
      if declared_forward "$adr" "$entry"; then
        if [[ "$mode" == "sweep" ]]; then
          echo "WRONG GLOB: ${base_name} -- '${entry}' is declared as created by" >&2
          echo "            this work, but the work is over and it governs nothing." >&2
          status=3
        else
          echo "Note: ${base_name} '${entry}' matches nothing yet, declared as" >&2
          echo "      created by this work. Taken at its word until close-out." >&2
        fi
      else
        echo "WARN: ${base_name} '${entry}' matches no tracked file and nothing" >&2
        echo "      says it should. The ADR governs nothing -- wrong glob, or a" >&2
        echo "      path that needs the 'created by this work' note." >&2
        [[ "$status" -eq 3 ]] || status=2
      fi
      continue
    fi

    # It matches real files. In sweep that retires a note; otherwise it is a
    # candidate for governing the change under test.
    if [[ "$mode" == "sweep" ]]; then
      if declared_forward "$adr" "$entry"; then
        echo "LIVE: ${base_name} '${entry}' now matches tracked files -- drop the" >&2
        echo "      'created by this work' note." >&2
      fi
      continue
    fi

    if grep -qxF -f "$work/target" <(printf '%s\n' "$tracked") 2>/dev/null; then
      hit=true
    fi
  done

  if [[ "$hit" == true ]]; then
    echo "governs this change: ${base_name}"
    governing=$(( governing + 1 ))
  fi
done
shopt -u nullglob

# ---- the report -------------------------------------------------------------

if [[ "$mode" != "sweep" && "$governing" -eq 0 ]]; then
  echo "No ADR with applies_to governs this change."
fi

if [[ "$quiet" != true && "${#unscoped[@]}" -gt 0 ]]; then
  echo ""
  echo "UNSCOPED -- ${#unscoped[@]} ADR(s) carry no applies_to, so no glob can"
  echo "match them. Read each one's decision and the paths it names in prose,"
  echo "and judge relevance by hand. An inherited decision binds just as hard"
  echo "as a scoped one, and 'zero ADRs govern this change' is a conclusion"
  echo "only once these have been read:"
  printf '  %s\n' "${unscoped[@]}"
  if [[ "$scoped" -eq 0 ]]; then
    echo ""
    echo "NOTE: no ADR in ${dir} carries applies_to at all, so the match above"
    echo "      had nothing to run against. The list is the whole answer here."
  fi
fi

exit "$status"
