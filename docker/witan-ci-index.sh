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
#   WITAN_CODE_SERVER      (required) omnigraph-server base URL
#   WITAN_CODE_TOKEN       (required) the svc-witan-ci bearer token
#   WITAN_CODE_CI_WORKDIR  scratch dir for checkouts (default /tmp/witan-ci-index)
#   WITAN_CODE_CI_ALLOW_LOCAL_STORE=1
#                          waive the two requirements above and index into
#                          local .omni directories. For testing this script;
#                          never for the deployed job.
#   WITAN_CODE_GITHUB_APP_ID / _INSTALLATION_ID / _KEY_FILE
#                          all three, or none: a GitHub App whose installation
#                          token authenticates the clones. Without them repos
#                          are cloned anonymously, which is correct when every
#                          managed repo is public. See witan_code/github_app.py.
#   WITAN_CODE_CI_ALLOW_PRIVATE_REPOS=1
#                          index private repos into shared graphs anyway. See
#                          the refusal in the loop below for why that is not
#                          the default, and what has to exist before it is.

set -eu

: "${WITAN_CODE_CI_REPOS:?set it to the whitespace-separated repo URIs to index}"

# Required, because the way witan-code handles their absence is to succeed.
# With no server configured, `witan code index` resolves each repo to a local
# `<slug>.omni` directory, creates it, indexes into it, and reports the usual
# scanned/indexed counts — inside a container whose filesystem is discarded
# when the pod exits, while the shared graphs this job exists to write go one
# more interval stale. An empty value does it too: witan_code.config._first
# skips falsy values, so a secret that synced blank is the same as no secret.
#
# Checking for them here is exact rather than approximate: this image ships no
# config.toml and sets no WITAN_CONFIG, so the environment is the only place
# witan-code can learn about a server, and what the shell can see is all
# there is.
if [ "${WITAN_CODE_CI_ALLOW_LOCAL_STORE:-}" != "1" ]; then
    : "${WITAN_CODE_SERVER:?set it to the omnigraph-server base URL (or set WITAN_CODE_CI_ALLOW_LOCAL_STORE=1 to index into local stores)}"
    : "${WITAN_CODE_TOKEN:?set it to the omnigraph bearer token for this indexer (or set WITAN_CODE_CI_ALLOW_LOCAL_STORE=1 to index into local stores)}"
fi

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

# This path is `rm -rf`'d twice per run and once per repo, so it is checked
# rather than trusted. Required: absolute, at least two components deep, no
# `..` or empty component. That rejects `/`, a bare `/tmp` (a shared directory
# this script has no business emptying), and anything that could climb out of
# where it was pointed. The deployment passes a constant, so this fires only
# for a mistake — which is exactly the case where the cost of not noticing is
# unbounded.
case "${workdir}" in
    *..* | *//*)
        echo "witan-ci-index: refusing WITAN_CODE_CI_WORKDIR=${workdir}:" \
             "no '..' or empty path components" >&2
        exit 1
        ;;
    /*/?*) ;;
    *)
        echo "witan-ci-index: refusing WITAN_CODE_CI_WORKDIR=${workdir}:" \
             "must be an absolute path at least two components deep" >&2
        exit 1
        ;;
esac

rm -rf "${workdir}"
mkdir -p "${workdir}"

# ── GitHub App authentication (optional) ─────────────────────────────────────
#
# Asked once, up front, so a broken credential fails before the first clone
# rather than as N identical clone errors. Three outcomes, deliberately
# distinct: configured, not configured (every managed repo is public — clone
# anonymously), and misconfigured, which is an error rather than a silent
# downgrade to anonymous.
github_app=0
check_status=0
python -m witan_code.github_app --check || check_status=$?
case ${check_status} in
    0) github_app=1 ;;
    2) : ;;         # EXIT_NOT_CONFIGURED — no App, clone anonymously
    *) exit 1 ;;    # EXIT_ERROR — it already said what is wrong, on stderr
esac

if [ "${github_app}" = "1" ]; then
    # The token reaches git through a credential helper reading the
    # environment, never through the clone URL. A URL-embedded credential ends
    # up in `origin`, and git echoes the remote URL in its own error messages —
    # so the first failed clone would print the token into the job log.
    #
    # The single quotes are the point, so SC2016 is wrong here: git stores
    # this string and runs it per credential request, and the token has to be
    # read *then* — it changes once per repo. Expanding it now would freeze
    # the first repo's token into the global gitconfig, where it would be both
    # stale for every later repo and persisted to disk.
    # shellcheck disable=SC2016
    git config --global credential.helper \
        '!f() { echo username=x-access-token; echo "password=${WITAN_CODE_GH_TOKEN}"; }; f'
    echo "witan-ci-index: authenticating clones as GitHub App installation" \
         "${WITAN_CODE_GITHUB_APP_INSTALLATION_ID}"
fi

allow_private="${WITAN_CODE_CI_ALLOW_PRIVATE_REPOS:-}"

indexed=0
failed=0
# The names, not just the count. `indexed 13, failed 1` is the LAST line of the
# job log and the first thing an operator reads, and it does not say which of
# fourteen repos is a run staler than it should be — so answering that meant
# scrolling back through fourteen repos' output to find the one `index failed
# for` line. Cheap to carry, and it is the difference between a summary that
# ends the investigation and one that starts it.
failed_repos=""

for repo in ${WITAN_CODE_CI_REPOS}; do
    # One checkout at a time, reusing the same path: holding every repo at once
    # would need disk proportional to the whole fleet, and the graph — not the
    # working tree — is what the run produces.
    checkout="${workdir}/checkout"
    rm -rf "${checkout}"

    # A fresh token per repo, not one for the sweep. An installation token is
    # valid for an hour; a cold run — every repo's first index, each parsed
    # from scratch — is allowed three. Minting once up front would work in
    # every test worth writing and then 401 partway through the first real
    # run, with the repos early in the list indexed and the rest not. One API
    # call against the cost of cloning and parsing a repo is nothing.
    if [ "${github_app}" = "1" ]; then
        if ! WITAN_CODE_GH_TOKEN="$(python -m witan_code.github_app)"; then
            echo "witan-ci-index: could not mint a token for ${repo}" >&2
            failed=$((failed + 1))
            failed_repos="${failed_repos} ${repo}"
            continue
        fi
        export WITAN_CODE_GH_TOKEN
    fi

    # ── Private repos are refused, and this is the only thing refusing them ──
    #
    # A shared code graph has no read scoping: `cluster.yaml` declares no
    # `policy:` block, so any bearer token the server accepts can read every
    # graph it serves. That costs nothing while every managed repo is public —
    # and it is exactly the moment a private one is indexed that it starts
    # costing, because the graph carries that repo's file paths, symbol names
    # and call structure to every witan user. The App's installation narrows
    # who can CLONE, never who can read the result.
    #
    # So the guard sits here, on the write path, rather than on the reviewed
    # list in ol-infrastructure's `managed_repos`: this is where a repo
    # actually becomes readable by everyone, whatever put it in the list.
    #
    # Only meaningful on the App path — cloning anonymously, a private repo
    # fails at `git clone` regardless, and asking GitHub about it needs a
    # credential this job would not have.
    #
    # Lift it by implementing per-repo read scoping
    # (tk-per-repo-read-scoping-on-code-graphs-via-github--371b4d), not by
    # setting the override, which exists so the decision to accept the
    # exposure has to be written down in the deployment and reviewed.
    if [ "${github_app}" = "1" ] && [ "${allow_private}" != "1" ]; then
        if ! visibility="$(python -m witan_code.github_app --visibility "${repo}")"
        then
            # Deliberately not "assume public and carry on": the question went
            # unanswered, and the failure mode of guessing is the one this
            # guard exists to prevent.
            echo "witan-ci-index: could not determine whether ${repo} is" \
                 "private; refusing to index it" >&2
            failed=$((failed + 1))
            failed_repos="${failed_repos} ${repo}"
            continue
        fi
        if [ "${visibility}" = "private" ]; then
            echo "witan-ci-index: refusing to index private repo ${repo} into" \
                 "a shared graph: code graphs have no per-repo read scoping," \
                 "so every witan user would be able to read it. Set" \
                 "WITAN_CODE_CI_ALLOW_PRIVATE_REPOS=1 to accept that." >&2
            failed=$((failed + 1))
            failed_repos="${failed_repos} ${repo}"
            continue
        fi
    fi

    echo "witan-ci-index: cloning ${repo}"
    if ! git clone --quiet --depth 1 --no-tags "${repo}" "${checkout}"; then
        echo "witan-ci-index: clone failed for ${repo}" >&2
        failed=$((failed + 1))
        failed_repos="${failed_repos} ${repo}"
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
        failed_repos="${failed_repos} ${repo}"
    fi
done

rm -rf "${workdir}"

echo "witan-ci-index: indexed ${indexed}, failed ${failed}${failed_repos:+ —${failed_repos}}"
# A repo that did not index has a shared view one run staler, which is a real
# failure — but it is not a reason to skip the rest of the fleet, so the exit
# status is decided after the sweep rather than inside it.
#
# ★ AND IT STAYS NON-ZERO for a partial sweep. A Job that reports Complete when
# one repo is stale is a Job nothing can alert on, and there is no other signal:
# the three consecutive silent failures from 2026-08-07 went unnoticed for two
# days precisely because a stale index has no symptom a reader would notice.
# The cost is that a 13-of-14 run looks as broken as a 0-of-14 one, which is
# what the repo names above are for.
[ "${failed}" -eq 0 ]
