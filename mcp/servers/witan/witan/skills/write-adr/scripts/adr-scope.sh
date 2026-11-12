#!/usr/bin/env bash
# Which ADRs govern a change, and whether their `applies_to` globs still work.
#
# Four checks used to sit inlined across three skills -- implement-plan Step 1
# (match the diff, list the unscoped), Step 6.3 (re-check a forward-looking
# note) and complete-project Step 3 (the same re-check, as a verdict). They had
# already drifted apart, and all of it was shell an agent retyped by hand.
#
# Three things this gets right that the obvious version does not:
#
# 1. `:(glob)` pathspec semantics are applied HERE, so a caller cannot forget
#    the prefix. Without it `*` crosses `/`: `git ls-files -- 'mcp/*.py'`
#    returns 211 files where `:(glob)mcp/*.py` returns 0. A narrow-looking glob
#    silently matching a whole subtree is the failure the prose keeps warning
#    about, and the warning does not work. Applying the prefix is only half of
#    it -- `--paths` splits its argument with globbing OFF, because a shell
#    that pathname-expands `docs/*` first hands this script `docs/sub`, and the
#    subtree is back.
#
# 2. `--changed` matches the pathspec against the DIFF, not against the index
#    intersected with it. A diff names paths the change deletes, and those are
#    gone from the index, so the intersection answered "no ADR governs this"
#    on a commit that removes files an ADR governs.
#
# 3. The UNSCOPED list is printed by every mode, not as a footnote. `applies_to`
#    is new: 0 of witan's 11 ADRs carry it today, so "no ADR governs this
#    change" is routinely what a correct run prints while four inherited
#    decisions bind just as hard. Zero governing ADRs is a conclusion only
#    after a human has read the unscoped ones.
#
# 4. A Superseded, Rejected or Deprecated record is read as HISTORY, not as a
#    broken scope. Its glob describes what the decision governed while it was
#    in force, and the record that replaced it is free to have deleted those
#    paths -- so demanding a live match turns every later run into a failure
#    on a record that is behaving exactly as the revision protocol requires.
#    It is reported, with a pointer to the record now in force, and it never
#    sets a non-zero exit.
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
#   --records "<a> [<b>...]"
#                    The records THIS work wrote, by filename or number. Only
#                    these can fail the run. Without it every record in the
#                    directory can, and a sweep run to close one project
#                    reports another project's in-flight forward-looking glob
#                    as WRONG GLOB -- a repair to a record whose work has not
#                    finished. Everything outside the set is still walked,
#                    still matched, and still listed; it just never becomes a
#                    verdict.
#   --quiet          Suppress the unscoped list. For a second run in the same
#                    session, never for the first.
#
# Exit: 0 clean; 1 usage or cannot run; 2 a CURRENT record's glob matches
#       nothing and nothing says it should; 3 (--sweep only) a forward-looking
#       note is now proven wrong -- the work is over and it governs nothing.
#       A superseded, rejected or deprecated record raises neither. Its glob
#       is a historical scope, so it is reported with a pointer to the record
#       now in force and nothing is asked of it.
set -euo pipefail

dir="docs/adr"
mode=""
base=""
paths=""
records=""
quiet=false

usage() {
  sed -n '/^# Usage:/,/^#       now in force/p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)     dir="${2:?--dir needs a path}"; shift 2 ;;
    --changed) mode="changed"; base="${2:?--changed needs a base ref}"; shift 2 ;;
    --paths)   mode="paths";  paths="${2:?--paths needs one or more globs}"; shift 2 ;;
    --sweep)   mode="sweep"; shift ;;
    --records) records="${2:?--records needs one or more record names}"; shift 2 ;;
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

# Split with globbing OFF, same reason as `--paths` below: an unquoted
# `0014-*.md` would otherwise be pathname-expanded against the CWD, which is
# not the ADR directory, and silently come through as the literal pattern.
declare -a record_set=()
if [[ -n "$records" ]]; then
  set -f
  # shellcheck disable=SC2206  # word splitting on purpose; globbing is off
  record_set=( $records )
  set +f
fi

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

# The status line's value, in either flavor -- `**Status:** Accepted` and
# `- Status: Accepted 2026-10-14 by @handle` -- or nothing. First match only:
# a record that discusses statuses in prose has plenty of later lines that
# look like this one.
status_of() {
  sed -n \
    -e 's/^[[:space:]]*\*\*Status:\*\*[[:space:]]*\(.*\)/\1/p' \
    -e 's/^[[:space:]]*-[[:space:]]*Status:[[:space:]]*\(.*\)/\1/p' \
    "$1" | head -1
}

# A record that no longer states current truth: superseded, rejected, or
# deprecated. Its `applies_to` is a HISTORICAL scope -- it says what the
# decision governed when it was in force, and the superseding decision is
# free to have removed those paths entirely. Holding it to a glob that still
# matches would make every later sweep fail on a record that is doing exactly
# what the revision protocol asks of it: staying put, with a pointer on top.
historical() {
  case "$1" in
    [Ss]uperseded*|[Rr]ejected*|[Dd]eprecated*) return 0 ;;
    *) return 1 ;;
  esac
}

# Is <basename> one of the records this work wrote? Always true when
# `--records` was not given, which is the standalone case: nobody named a
# set, so every record in the directory is in scope.
#
# A name matches on the leading digits, so `--records 0014` and
# `--records 0014-csp.md` and `--records ADR-0014` all name the same file. The
# number is what everything else in this bundle carries -- `Supersedes
# ADR-0014`, a `Touches:` entry, the index table -- and requiring the filename
# would mean looking it up to pass it.
in_record_set() {
  [[ "${#record_set[@]}" -gt 0 ]] || return 0
  local want got
  got="${1%%-*}"; got="${got%.md}"
  for want in "${record_set[@]}"; do
    want="${want##*/}"
    want="${want#[Aa][Dd][Rr]-}"
    want="${want%%-*}"; want="${want%.md}"
    [[ "$want" == "$got" ]] && return 0
  done
  return 1
}

# Every tracked file an entry matches, with :(glob) applied here so no caller
# can forget it.
files_matching() {
  git ls-files -- ":(glob)$1" 2>/dev/null || true
}

# Every path in the diff an entry matches. Not `git ls-files` intersected with
# the diff: a diff names paths the change DELETES, and those are by definition
# not in the index any more, so an intersection reports "no ADR governs this"
# on a commit whose whole content is removing files an ADR governs. Let git
# apply the pathspec to the diff itself, which sees adds, deletes and renames
# alike.
diff_matching() {
  git diff --name-only "$base"...HEAD -- ":(glob)$1" 2>/dev/null || true
}

# ---- the sets a mode compares against ---------------------------------------

case "$mode" in
  changed)
    # Nothing is collected here: the match runs per entry, through
    # `diff_matching`, so git applies the pathspec to the diff. This is only
    # the once-per-run check that the diff resolves at all, because the
    # per-entry calls swallow their errors to keep one bad glob from aborting
    # the walk.
    git diff --name-only "$base"...HEAD >/dev/null || {
      echo "Error: could not diff ${base}...HEAD" >&2; exit 1; }
    ;;
  paths)
    : > "$work/target"
    # `set -f` for the split, because the shell would otherwise PATHNAME-EXPAND
    # each glob before this loop ever sees it -- defeating the whole point of
    # applying `:(glob)` below. Unquoted `$paths` is deliberate word splitting;
    # unquoted `$paths` with globbing on is `docs/*` quietly becoming
    # `docs/adr docs/sub docs/top.md`, and `docs/sub` as a pathspec then drags
    # in a whole subtree that `:(glob)docs/*` never matches.
    set -f
    # shellcheck disable=SC2206  # word splitting on purpose; globbing is off
    path_globs=( $paths )
    set +f
    for g in "${path_globs[@]}"; do
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
  record_status="$(status_of "$adr")"
  record_status="${record_status:-no status line}"
  # `Superseded by ADR-0012` carries its own pointer; `Rejected` and
  # `Deprecated` do not, and a flavor that keeps it on a separate line is
  # read here too.
  supersedes_to="$(
    printf '%s\n' "$record_status"
    sed -n 's/^[[:space:]]*\*\*Superseded By:\*\*[[:space:]]*//p' "$adr"
    sed -n 's/^[[:space:]]*-[[:space:]]*Superseded by:[[:space:]]*//p' "$adr"
  )"
  supersedes_to="$(printf '%s' "$supersedes_to" | grep -oE 'ADR-?[0-9]+' | head -1 || true)"
  if [[ -n "$supersedes_to" ]]; then
    successor="read ${supersedes_to} for the constraint in force"
  else
    # Rejected and Deprecated name no replacement, and neither does a
    # Superseded record whose pointer was never written -- which is itself
    # worth saying, because that record stays silently authoritative to
    # whoever finds it first.
    successor="no replacement is named on it"
  fi
  mapfile -t entries < <(applies_to_of "$adr")

  if [[ "${#entries[@]}" -eq 0 ]]; then
    unscoped+=("$base_name")
    continue
  fi
  scoped=$(( scoped + 1 ))

  hit=false
  for entry in "${entries[@]}"; do
    tracked="$(files_matching "$entry")"

    # In --changed the question is which ADRs the DIFF touches, so ask the
    # diff. Done before the empty-tree branch below, because a change that
    # deletes every file an entry governed leaves nothing for `git ls-files`
    # to find and is exactly the change that ADR most needs read.
    in_diff=false
    if [[ "$mode" == "changed" && -n "$(diff_matching "$entry")" ]]; then
      in_diff=true
      hit=true
    fi

    # An entry matching nothing in the tree. Four causes, and only the mode
    # and the record's own status know which is a verdict.
    if [[ -z "$tracked" ]]; then
      if [[ "$in_diff" == true ]]; then
        echo "Note: ${base_name} '${entry}' matches no tracked file because THIS" >&2
        echo "      change deletes every file it governed. The glob is not wrong;" >&2
        echo "      the decision may now be obsolete -- say so in the pull request." >&2
      elif historical "$record_status"; then
        echo "HISTORICAL: ${base_name} (${record_status}) '${entry}' matches" >&2
        echo "            nothing. That is a scope as it WAS, and a superseding" >&2
        echo "            decision is free to have removed those paths. Nothing" >&2
        echo "            to repair here -- ${successor}." >&2
      elif declared_forward "$adr" "$entry"; then
        if [[ "$mode" == "sweep" ]] && in_record_set "$base_name"; then
          echo "WRONG GLOB: ${base_name} -- '${entry}' is declared as created by" >&2
          echo "            this work, but the work is over and it governs nothing." >&2
          status=3
        elif [[ "$mode" == "sweep" ]]; then
          echo "Note: ${base_name} '${entry}' matches nothing yet, declared as" >&2
          echo "      created by work that is not this one. Its sweep, not ours." >&2
        else
          echo "Note: ${base_name} '${entry}' matches nothing yet, declared as" >&2
          echo "      created by this work. Taken at its word until close-out." >&2
        fi
      elif in_record_set "$base_name"; then
        echo "WARN: ${base_name} '${entry}' matches no tracked file and nothing" >&2
        echo "      says it should. The ADR governs nothing -- wrong glob, or a" >&2
        echo "      path that needs the 'created by this work' note." >&2
        [[ "$status" -eq 3 ]] || status=2
      else
        echo "Note: ${base_name} '${entry}' matches no tracked file. Outside the" >&2
        echo "      records this work wrote, so it is reported, not repaired." >&2
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

    if [[ "$mode" == "paths" ]] \
       && grep -qxF -f "$work/target" <(printf '%s\n' "$tracked") 2>/dev/null; then
      hit=true
    fi
  done

  if [[ "$hit" == true ]]; then
    if historical "$record_status"; then
      # Still printed: the revision protocol has a reader follow the chain
      # rather than stop at whichever record they found first. But labelled,
      # because taking a constraint from here implements a decision that was
      # replaced.
      echo "governs this change: ${base_name} -- ${record_status};" \
           "${successor}."
    else
      echo "governs this change: ${base_name}"
    fi
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
