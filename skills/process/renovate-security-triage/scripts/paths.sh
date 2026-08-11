#!/usr/bin/env bash
# Shared default file locations for the renovate-security-triage pipeline.
# Sourced by every script here; not executable on its own.
#
# Why this exists: the five scripts hand JSON to each other, and if the *caller*
# has to name those files then the bash command string changes between runs --
# which means a permission-allowlist entry approved once never matches again.
# (Agent harnesses that rewrite /tmp into a session-scoped scratchpad directory
# are the usual culprit: the session UUID is new every time.) Defaulting the
# paths here keeps the documented invocation byte-identical across runs, so it
# can be approved once.
#
# Callers may still pass explicit paths for ad-hoc use, and
# RENOVATE_TRIAGE_DIR relocates the whole set without changing any command.

# shellcheck disable=SC2034  # every var here is consumed by the sourcing script
triage_dir="${RENOVATE_TRIAGE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/renovate-security-triage}"
mkdir -p "$triage_dir"

# One directory, not one per org -- the skill scopes a run to a single org, and
# each run overwrites the previous. Retained (rather than piped) so a failure in
# the expensive enrich/advisory phases can be resumed by re-running just the
# phase that died, and so odd rankings can be traced back to parsed input.
active_json="${triage_dir}/active.json"
renovate_json="${triage_dir}/renovate.json"
enriched_json="${triage_dir}/enriched.json"
scored_json="${triage_dir}/scored.json"
classified_json="${triage_dir}/classified.json"
