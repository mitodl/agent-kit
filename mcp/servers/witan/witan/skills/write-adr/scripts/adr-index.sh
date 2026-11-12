#!/usr/bin/env bash
# Derive the ADR index table from the ADR files themselves.
#
# The index is a pure function of the files, but it is maintained by hand and
# has already drifted: ol-infrastructure's README lists 0005 as 2025-11-20
# while the file says 2025-11-21, and drops the tail of 0002's title. Deriving
# it removes a class of rot rather than asking people to be careful.
#
# Two modes on purpose. --verify is the default because regenerating on first
# contact rewrites pre-existing wrong rows inside a pull request about
# something else, where it reads as unexplained noise. Fix drift in its own
# change, then let later runs regenerate.
#
# Usage:
#   adr-index.sh [--verify|--print] [adr-dir]
#
#   --verify   (default) Print the table and report rows that disagree with
#              README.md. Exit 2 if any disagree.
#   --print    Print the table only, for pasting into README.md.
#
# Requires: git (for nothing but being in a repo), sed, grep
set -euo pipefail

mode="verify"
dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --verify) mode="verify"; shift ;;
    --print)  mode="print";  shift ;;
    -h|--help) sed -n '2,20p' "$0" >&2; exit 1 ;;
    *) dir="$1"; shift ;;
  esac
done
dir="${dir:-docs/adr}"
[[ -d "$dir" ]] || { echo "Error: no such directory: ${dir}" >&2; exit 1; }

# One metadata value, across the three shapes these repos use:
#
#   **Status:** Accepted        ol-infrastructure
#   - Status: Accepted          witan
#   | **Status** | Accepted |   table
#
# The table form is not decoration. Consecutive lines are one paragraph in
# Markdown, so ol-infrastructure's four bolded lines render as a single run-on
# sentence everywhere they are read -- GitHub, an editor preview, a docs site.
# A repo that noticed will have reached for a list or a table, and reading only
# the first two shapes fills its index with UNKNOWN while reporting success.
#
# `|| return 0` rather than letting grep's exit 1 propagate: under `set -e` a
# missing field is a blank cell, not a failed run.
meta_field() {
  local file=$1 key=$2 line
  line=$(grep -m1 -E "^([[:space:]]*\*\*${key}:\*\*|[[:space:]]*- ${key}:|\|[[:space:]]*\*{0,2}${key}\*{0,2}[[:space:]]*\|)" "$file") || return 0
  case "$line" in
    # Table row: the value is the second cell, so drop through two pipes and
    # cut at the third. An unescaped pipe inside a cell is a column separator
    # in Markdown too, so cutting at the first one is the same answer the
    # renderer gives -- a value that really contains one has to be written
    # `\|`, and would be truncated here. Neither Status nor Date ever is.
    \|*) line=${line#*|}; line=${line#*|}; line=${line%%|*} ;;
    *)   line=$(printf '%s' "$line" | sed -E "s/^[[:space:]]*(\*\*${key}:\*\*|- ${key}:)[[:space:]]*//") ;;
  esac
  printf '%s' "$line" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//'
}

# One row per ADR.
emit_table() {
  printf '| ADR | Title | Status | Date |\n'
  printf '|-----|-------|--------|------|\n'
  for f in "$dir"/[0-9]*.md; do
    [[ -e "$f" ]] || continue
    base="${f##*/}"
    # Accept NNNN-slug.md and a bare NNNN.md alike -- next-adr-number.sh counts
    # both, and an index that silently drops one reports itself clean while
    # missing an ADR.
    num="${base%%-*}"; num="${num%.md}"
    [[ "$num" =~ ^[0-9]+$ ]] || continue
    # Read the H1 past any YAML frontmatter: this skill mandates an
    # applies_to block, so line 1 is '---', not the heading. Reading line 1
    # yields an empty title and the index fills with UNKNOWN forever.
    title=$(sed -e '/./,$!d' "$f" \
            | awk 'NR==1 && $0=="---" {fm=1; next} fm && $0=="---" {fm=0; next} !fm' \
            | sed -n '/^# /{s/^# *[0-9]*\. *//p;q}')
    status=$(meta_field "$f" Status | cut -c1-60)
    date=$(meta_field "$f" Date)
    printf '| [%s](%s) | %s | %s | %s |\n' \
      "$num" "$base" "${title:-UNKNOWN}" "${status:-UNKNOWN}" "${date:-UNKNOWN}"
  done
}

table="$(emit_table)"

if [[ "$mode" == "print" ]]; then
  printf '%s\n' "$table"
  exit 0
fi

printf '%s\n' "$table"

readme="${dir}/README.md"
[[ -f "$readme" ]] || { echo "Note: no ${readme} to compare against." >&2; exit 0; }

# Compare on content, not on column alignment. A README that has been through
# Prettier -- or any hand-aligned table -- pads every cell to the widest one,
# so an exact substring match reports drift on rows that are character-for-
# character correct once the padding is removed. That false positive is worse
# than no check: it fires on every run, so it trains people to ignore the one
# time it is real.
norm() { sed -E 's/[[:space:]]+/ /g; s/[[:space:]]*\|[[:space:]]*/|/g; s/^ +//; s/ +$//'; }
readme_norm="$(norm < "$readme")"

drift=0
while IFS= read -r row; do
  [[ "$row" == \|\ \[* ]] || continue
  link="${row#*(}"; link="${link%%)*}"          # the filename the row links to
  printf '%s\n' "$readme_norm" | grep -qF -- "$(printf '%s' "$row" | norm)" || {
    if grep -qF -- "($link)" "$readme"; then
      echo "Drift: ${link} row in README disagrees with the file." >&2
      drift=1
    else
      echo "Missing: ${link} has no row in README." >&2
      drift=1
    fi
  }
done <<< "$table"

if (( drift )); then
  echo "" >&2
  echo "README index disagrees with the files. Fix it in its own change, not" >&2
  echo "inside an unrelated PR -- then later runs can regenerate cleanly." >&2
  exit 2
fi
echo "README index matches the files." >&2
