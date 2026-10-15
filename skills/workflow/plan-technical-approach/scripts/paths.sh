# shellcheck shell=bash
# Shared file locations for the plan-comment scripts.
#
# Sourced by the scripts here, and by implement-spec, which updates the same
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
