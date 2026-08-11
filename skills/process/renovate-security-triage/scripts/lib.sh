#!/usr/bin/env bash
# Shared helpers for the renovate-security-triage scripts. Sourced by every
# script here (which also pulls in paths.sh); not executable on its own.

lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./paths.sh
source "${lib_dir}/paths.sh"

# The heavy data transforms live as standalone jq programs in this directory,
# run with `jq -f` (or included as a module via `jq -L`), so the .sh files
# stay orchestration-only.
# shellcheck disable=SC2034  # consumed by the sourcing scripts
jq_dir="${lib_dir}/jq"

max_parallel=8

# Block until a background-job slot (out of $max_parallel) is free.
#
# `wait -n` is bash 4.3+; macOS still ships bash 3.2 as /bin/bash, and these
# scripts otherwise stay 3.2-clean (see also the BSD `date` fallback in
# active-repos.sh), so poll there instead of hard-failing on a stock shell.
if ((BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 3))); then
  wait_for_slot() {
    while [[ "$(jobs -r -p | wc -l)" -ge "$max_parallel" ]]; do
      wait -n || true
    done
  }
else
  wait_for_slot() {
    while [[ "$(jobs -r -p | wc -l)" -ge "$max_parallel" ]]; do
      sleep 0.2
    done
  }
fi

# gather_json <dir> <prefix> <dest> <reducer>
# Combine <dir>/<prefix>-*.json into <dest> with `jq -s <reducer>`, after
# sweeping away empty files (failed fetches). Writes [] when nothing matched.
#
# nullglob rather than `compgen -G`: some bash builds (nixpkgs bash 5.3) omit
# the compgen builtin from non-interactive shells, where it fails as "command
# not found" -- and under `set -e` inside an `if` that silently reads as "no
# matching files".
gather_json() {
  local dir="$1" prefix="$2" dest="$3" reducer="$4"
  find "$dir" -name "${prefix}-*.json" -empty -delete
  shopt -s nullglob
  local files=("${dir}/${prefix}"-*.json)
  shopt -u nullglob
  if [[ "${#files[@]}" -gt 0 ]]; then
    jq -s "$reducer" "${files[@]}" > "$dest"
  else
    echo '[]' > "$dest"
  fi
}
