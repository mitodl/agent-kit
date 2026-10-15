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

# One row per ADR. Handles both flavors' metadata lines: ol-infrastructure's
# bolded "**Status:**" and witan's bullet "- Status:".
emit_table() {
  printf '| ADR | Title | Status | Date |\n'
  printf '|-----|-------|--------|------|\n'
  for f in "$dir"/[0-9]*.md; do
    [[ -e "$f" ]] || continue
    base="${f##*/}"
    num="${base%%-*}"
    [[ "$num" =~ ^[0-9]+$ ]] || continue
    title=$(sed -n '1s/^# *[0-9]*\. *//p' "$f")
    status=$(grep -m1 -E '^(\*\*Status:\*\*|- Status:)' "$f" \
             | sed -E 's/^(\*\*Status:\*\*|- Status:) *//' | cut -c1-60)
    date=$(grep -m1 -E '^(\*\*Date:\*\*|- Date:)' "$f" \
           | sed -E 's/^(\*\*Date:\*\*|- Date:) *//')
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

drift=0
while IFS= read -r row; do
  [[ "$row" == \|\ \[* ]] || continue
  link="${row#*(}"; link="${link%%)*}"          # the filename the row links to
  grep -qF -- "$row" "$readme" || {
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
