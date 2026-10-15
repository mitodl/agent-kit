#!/usr/bin/env bash
# Next ADR number for a directory, plus a consistency check of what is there.
#
# The number comes from the FILESYSTEM, never from the index table. Every
# hand-maintained pointer in these repos has rotted -- the ol-infrastructure
# index disagrees with its files on dates and titles, its agent guide still
# says "Current sequence: 0002" with thirteen ADRs on disk, and agent-kit's
# docs nav lists 0008 before 0005. The files are the only source that cannot
# drift from itself.
#
# It also reports filename/H1 mismatches, because that bug is already in the
# corpus: ol-infrastructure's 0006-label-based-slack-channel-routing-for-
# kubewatch.md opens with "# 0003." and has since 2025-11.
#
# Usage:
#   next-adr-number.sh [adr-dir]     # default: ./docs/adr
#
# Output: the zero-padded next number on stdout; warnings on stderr.
# Exit: 0 normally, 2 if a mismatch or duplicate number was found.
set -euo pipefail

dir="${1:-docs/adr}"
[[ -d "$dir" ]] || { echo "Error: no such directory: ${dir}" >&2; exit 1; }

status=0
highest=0
declare -A seen

for f in "$dir"/[0-9]*.md; do
  [[ -e "$f" ]] || continue
  base="${f##*/}"
  # Accept both NNNN-slug.md and a bare NNNN.md. Stripping only at the first
  # dash leaves "0015.md" non-numeric, which previously skipped the file --
  # so the number it already occupies got handed out again.
  num="${base%%-*}"; num="${num%.md}"
  if [[ ! "$num" =~ ^[0-9]+$ ]]; then
    echo "Warning: ${base} does not start with a number; not counted" >&2
    status=2
    continue
  fi
  n=$((10#$num))

  if [[ -n "${seen[$n]:-}" ]]; then
    echo "Warning: number ${num} used twice: ${seen[$n]} and ${base}" >&2
    status=2
  fi
  seen[$n]="$base"
  (( n > highest )) && highest=$n

  # The H1 carries the number too; the two drift. Read it past any YAML
  # frontmatter -- this skill mandates one, so line 1 is '---' and a line-1
  # read makes this check silently never fire.
  h1=$(awk 'NR==1 && $0=="---" {fm=1; next} fm && $0=="---" {fm=0; next} !fm' "$f" \
       | sed -n '/^# /{s/^# *0*\([0-9][0-9]*\)\..*/\1/p;q}')
  if [[ -n "$h1" && "$((10#$h1))" -ne "$n" ]]; then
    echo "Warning: ${base} has H1 number ${h1}; filename says ${num}" >&2
    status=2
  fi
done

printf '%04d\n' "$((highest + 1))"
exit "$status"
