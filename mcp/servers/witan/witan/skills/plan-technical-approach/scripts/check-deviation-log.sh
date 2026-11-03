#!/usr/bin/env bash
# Validate a deviation log, and count the entries that moved the chunk graph.
#
# The log carries more rules than anything else in this bundle and, until this
# script, nothing checked any of them: six fields, 800 characters hard,
# append-only, newest first, a replan threshold of exactly three shape-changing
# entries, and an ADR list close-out has to supersede from.
#
# The threshold is the reason this exists rather than being left to prose.
# `implement-plan` says "count the entries rather than judging the feel of it"
# -- which only works if something counts. Each individual change looks small;
# the third is where repairing a plan stops being cheaper than rewriting it.
#
# Usage:
#   check-deviation-log.sh <path> [--since <ref>] [--list-adrs] [--quiet]
#
# Options:
#   --since <ref>  Also assert the file is append-only across <ref>..HEAD. This
#                  is the one rule only a diff can check, and the one an agent
#                  is likeliest to break by "tidying" an entry.
#   --list-adrs    Print every ADR named across all entries, one per line, and
#                  nothing else. This is what complete-project supersedes from.
#   --quiet        Findings only; no summary.
#
# Exit: 0 clean; 1 malformed, or the file is missing; 2 usage;
#       3 the replan threshold is reached -- stop repairing and go back to
#       plan-technical-approach, which rewrites the plan FROM these entries.
set -euo pipefail

path=""
since=""
list_adrs=false
quiet=false

usage() {
  sed -n '/^# Usage:/,/^#       plan-technical/p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)     since="${2:?--since needs a git ref}"; shift 2 ;;
    --list-adrs) list_adrs=true; shift ;;
    --quiet)     quiet=true; shift ;;
    -h|--help)   usage ;;
    -*) echo "Error: unknown argument: $1" >&2; usage ;;
    *)  [[ -z "$path" ]] || { echo "Error: more than one path given." >&2; usage; }
        path="$1"; shift ;;
  esac
done

[[ -n "$path" ]] || { echo "Error: a deviation-log path is required." >&2; usage; }
[[ -f "$path" ]] || {
  echo "Error: no deviation log at ${path}" >&2
  echo "No log means no deviations, which is a normal outcome -- but this was" >&2
  echo "asked for by path, so the path is wrong or the file was deleted early." >&2
  exit 1
}

# The six fields, in the order the entry must carry them.
FIELDS=(Assumed Found "Changed to" Touches "Approved by" PR)
CAP=800
THRESHOLD=3

status=0
entries=0
shape=0
adrs_file="$(mktemp)"
trap 'rm -f "$adrs_file"' EXIT

report() { [[ "$list_adrs" == true ]] || echo "$@" >&2; }
fail()   { report "$@"; [[ "$status" -eq 3 ]] || status=1; }

today="$(date -u +%Y-%m-%d)"
prev_date=""

# Split on the `### ` headings. Read the whole file once; these are bounded by
# construction (the cap, and the delete-at-close-out rule), so there is no
# streaming case to worry about.
heading=""
start=0
lineno=0
declare -a buf=()

check_entry() {
  [[ -n "$heading" ]] || return 0
  entries=$(( entries + 1 ))
  local body text date_part
  text="$(printf '%s\n' "$heading"; printf '%s\n' "${buf[@]}")"

  # --- the date, from the heading: `### YYYY-MM-DD — <summary>`
  date_part="$(printf '%s' "$heading" | sed -n 's/^###[[:space:]]*\([0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\).*/\1/p')"
  if [[ -z "$date_part" ]]; then
    fail "L${start}: entry heading carries no YYYY-MM-DD date: ${heading}"
  else
    if ! date -u -d "$date_part" >/dev/null 2>&1 \
       && ! date -u -j -f "%Y-%m-%d" "$date_part" >/dev/null 2>&1; then
      fail "L${start}: ${date_part} is not a real date."
    fi
    # An agent's idea of today is routinely wrong, which is why the format
    # mandates `date -u`. A future date is the tell.
    if [[ "$date_part" > "$today" ]]; then
      fail "L${start}: ${date_part} is in the future (today is ${today} UTC)."
    fi
    # Newest first.
    if [[ -n "$prev_date" && "$date_part" > "$prev_date" ]]; then
      fail "L${start}: ${date_part} is newer than the entry above it (${prev_date}); newest first."
    fi
    prev_date="$date_part"
  fi

  # --- the six fields, present and in order
  local want
  for want in "${FIELDS[@]}"; do
    printf '%s\n' "${buf[@]}" | grep -q "^- \*\*${want}:\*\*" \
      || fail "L${start}: missing field: **${want}:**"
  done

  # Order: the fields as they actually appear, against the canonical list
  # filtered to the ones present. Equal means in order.
  local present expected
  present="$(printf '%s\n' "${buf[@]}" \
    | sed -n 's/^- \*\*\(Assumed\|Found\|Changed to\|Touches\|Approved by\|PR\):\*\*.*/\1/p')"
  if [[ -n "$present" ]]; then
    expected="$(printf '%s\n' "${FIELDS[@]}" | grep -xF -f <(printf '%s\n' "$present") || true)"
    [[ "$present" == "$expected" ]] \
      || fail "L${start}: fields are out of order (got: $(echo "$present" | tr '\n' ',' ))."
  fi

  # --- a field spilling onto a second line
  if printf '%s\n' "${buf[@]}" | grep -qE '^[[:space:]]+[^-[:space:]]'; then
    fail "L${start}: a field continues onto a second line. One line each -- an" \
         "entry that does not fit is carrying reasoning."
  fi

  # --- the cap
  local chars
  chars="$(printf '%s' "$text" | LC_ALL="${plan_utf8_locale:-C.UTF-8}" wc -m | tr -d ' ')"
  if (( chars > CAP )); then
    fail "L${start}: entry is ${chars} characters; the cap is ${CAP}. What does" \
         "not fit belongs in the Dn. section or the ADR it changed."
  fi

  # --- what it touched
  body="$(printf '%s\n' "${buf[@]}" | sed -n 's/^- \*\*Touches:\*\*[[:space:]]*//p')"
  # A recognised `chunks` token is how "the delivery shape moved" becomes
  # countable without a seventh field. See references/deviation-log.md.
  if printf '%s' "$body" | grep -qiE '(^|[^[:alnum:]])chunks?([^[:alnum:]]|$)'; then
    shape=$(( shape + 1 ))
  fi
  printf '%s\n' "$body" | grep -oE 'ADR-[0-9]+' >> "$adrs_file" || true
}

while IFS= read -r line || [[ -n "$line" ]]; do
  lineno=$(( lineno + 1 ))
  if [[ "$line" == '### '* ]]; then
    check_entry
    heading="$line"; start="$lineno"; buf=()
  elif [[ -n "$heading" ]]; then
    buf+=("$line")
  fi
done < "$path"
check_entry

# ---- append-only, which only a diff can see --------------------------------

if [[ -n "$since" ]]; then
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    fail "--since needs a git work tree."
  elif ! git rev-parse --verify --quiet "$since" >/dev/null; then
    fail "--since: cannot resolve ref: ${since}"
  else
    # --numstat, not a grep over the diff markers. Every field in an entry is
    # a markdown list item starting `- `, so a REMOVED one reads as `-- **…`
    # in the diff -- and the obvious `^-[^-]` pattern, written to skip the
    # `--- a/path` header, skips all of them too. That check passed on a log
    # whose every line had been rewritten.
    removed="$(git diff --numstat "$since"...HEAD -- "$path" | awk '{print $2}')"
    if [[ "${removed:-0}" =~ ^[0-9]+$ ]] && (( removed > 0 )); then
      fail "${removed} line(s) were REMOVED from the log since ${since}. It is" \
           "append-only: a deviation later reversed gets a second entry saying" \
           "so, never an edit to the first."
    fi
  fi
fi

# ---- output -----------------------------------------------------------------

if [[ "$list_adrs" == true ]]; then
  sort -u "$adrs_file"
  exit "$status"
fi

if (( shape >= THRESHOLD )); then
  echo "" >&2
  echo "REPLAN: ${shape} entries changed the chunk graph's shape (threshold ${THRESHOLD})." >&2
  echo "Stop repairing the plan one deviation at a time and go back to" >&2
  echo "plan-technical-approach, which rewrites it FROM these entries. Past" >&2
  echo "this point you are patching a plan you no longer have." >&2
  status=3
fi

if [[ "$quiet" != true ]]; then
  echo "${entries} entr$([ "$entries" = 1 ] && echo y || echo ies), ${shape} of which changed the chunk graph's shape."
  if [[ -s "$adrs_file" ]]; then
    echo "ADRs these entries touched, to be superseded at close-out:"
    sed 's/^/  /' <(sort -u "$adrs_file")
  else
    echo "No ADR named in any entry -- nothing to supersede at close-out."
  fi
fi

exit "$status"
