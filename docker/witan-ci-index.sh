#!/bin/sh
# The CI code-graph indexer: the one entitled writer of every shared code
# graph's default view. Run as a Kubernetes CronJob by ol-infrastructure's
# `applications/witan/ci_indexer.py`, out of the same image the MCP tier
# serves from, so the code writing the graph is the code reading it.
#
# Each repo's shared graph has exactly one writer entitled to its default
# (`main`) view. witan-code refuses that write — and the stale-file purge that
# goes with it — from any process that has not declared
# `WITAN_CODE_INDEX_ROLE=ci` (witan_code/graph.py `check_writable`), so nothing
# else can update the view every reader falls back to, and nothing else keeps
# it from going stale either. This script is the other half of that guard.
#
# WHY IN-CLUSTER RATHER THAN GITHUB ACTIONS
#
# omnigraph-server is ClusterIP-only and is deliberately not getting an
# HTTPRoute (DECIDED 2026-08-01, witan_code/ingest.py) — the witan MCP tier is
# the one exposed boundary. Everything outside the cluster therefore reaches a
# code graph through that tier, one round trip per store operation: fine for
# the few-files-changed reindex a developer's branch does, not fine for the
# thousands a full-repo run makes. This job is the one writer that does
# full-repo runs routinely, so it runs where the direct `--server/--graph`
# path is reachable at all.
#
# WHY A FULL CHECKOUT
#
# A run indexes the whole default branch and purges rows for files it did not
# see, so a *sparse* checkout would delete every file it filtered out — for
# everyone. A *shallow* one is fine and is what this uses: `--depth 1`
# truncates history, not the working tree, and nothing in the indexer reads
# past HEAD.
#
# Environment:
#   WITAN_CODE_CI_REPOS    (required) whitespace-separated canonical repo URIs
#   WITAN_CODE_SERVER      omnigraph-server base URL
#   WITAN_CODE_TOKEN       the svc-witan-ci bearer token
#   WITAN_CODE_CI_WORKDIR  scratch dir for checkouts (default /tmp/witan-ci-index)

set -eu

: "${WITAN_CODE_CI_REPOS:?set it to the whitespace-separated repo URIs to index}"

# Declared, never inferred: witan-code treats the role as the authority on who
# may write a shared default view, so the job asserts it here rather than
# relying on the deployment to remember.
WITAN_CODE_INDEX_ROLE=ci
export WITAN_CODE_INDEX_ROLE

# WITAN_REPO overrides the repo detected from a checkout's git remote
# (witan_code/repo.py `detect`), which for a sweep over N checkouts means every
# one of them indexing into the same graph — repo A's symbols landing in repo
# B's shared view, with nothing failing to say so. A single value inherited
# from the pod environment would do it, so this loop does not run with one set.
unset WITAN_REPO

workdir="${WITAN_CODE_CI_WORKDIR:-/tmp/witan-ci-index}"
rm -rf "${workdir}"
mkdir -p "${workdir}"

indexed=0
failed=0

for repo in ${WITAN_CODE_CI_REPOS}; do
    # One checkout at a time, reusing the same path: holding every repo at once
    # would need disk proportional to the whole fleet, and the graph — not the
    # working tree — is what the run produces.
    checkout="${workdir}/checkout"
    rm -rf "${checkout}"

    echo "witan-ci-index: cloning ${repo}"
    if ! git clone --quiet --depth 1 --no-tags "${repo}" "${checkout}"; then
        echo "witan-ci-index: clone failed for ${repo}" >&2
        failed=$((failed + 1))
        continue
    fi

    # `--depth 1` implies `--single-branch`, which leaves no
    # refs/remotes/origin/HEAD for witan_code.repo._default_branch to read.
    # Without it that function falls back to "main or master, whichever is
    # present" — correct for every repo indexed today, and silently wrong for
    # the first one whose default is neither: its checkout would look like a
    # feature branch and index onto a branch view, leaving the shared view
    # untouched and no error to explain it. Non-fatal, because the fallback is
    # still right for the repos it covers.
    if ! git -C "${checkout}" remote set-head origin --auto >/dev/null 2>&1; then
        echo "witan-ci-index: could not resolve origin/HEAD for ${repo};" \
             "falling back to main/master detection" >&2
    fi

    echo "witan-ci-index: indexing ${repo}"
    if (cd "${checkout}" && witan code index .); then
        indexed=$((indexed + 1))
    else
        echo "witan-ci-index: index failed for ${repo}" >&2
        failed=$((failed + 1))
    fi
done

rm -rf "${workdir}"

echo "witan-ci-index: indexed ${indexed}, failed ${failed}"
# A repo that did not index has a shared view one run staler, which is a real
# failure — but it is not a reason to skip the rest of the fleet, so the exit
# status is decided after the sweep rather than inside it.
[ "${failed}" -eq 0 ]
