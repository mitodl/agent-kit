# Checks reference

GitHub exposes two different, overlapping mechanisms for a PR's green/red
status bar: the (newer) **Checks API**, used by GitHub Actions and most
GitHub Apps, and the (older) **Commit Status API**, still used by some
third-party integrations. `gh pr checks` — and `fetch-checks.sh`, which
wraps it — flattens both into one list, so you rarely need to care which one
a given check uses. This doc is for when you need to go beyond what the
script fetches.

## What `fetch-checks.sh` gives you

```bash
./skills/process/address-pr-feedback/scripts/fetch-checks.sh mitodl/agent-kit 116 /tmp/pr116-checks.json
```

Each entry in `checks[]` has:

- `name` — the check's display name (`"Analyze (python)"`, `"GitGuardian
  Security Checks"`, `"pre-commit.ci"`)
- `bucket` — `pass` / `fail` / `pending` / `skipping` / `cancel` (`gh`'s own
  normalization of the underlying `state`/`conclusion` fields — use this,
  not `state`, for pass/fail branching)
- `workflow` — the GitHub Actions workflow name, empty for non-Actions checks
- `link` — the details URL; for an Actions job this is
  `.../actions/runs/<run_id>/job/<job_id>`, for everything else it's
  wherever the third-party service hosts its own report
- `run_id` — added by `fetch-checks.sh` itself (not native `gh` output): the
  numeric Actions run ID extracted from `link`, or `null` if this isn't an
  Actions job

`action_run_logs[<run_id>]` holds the failed-step output for every failing
Actions run referenced by a check, fetched via `gh run view <run_id>
--log-failed` and truncated to the last ~20k characters. One workflow run
often backs several `checks[]` entries (one per job); the log is fetched
once per run, not once per check.

## Why third-party checks have no log to fetch

`gh run view --log-failed` only works for GitHub Actions runs — it reads
build logs GitHub itself stores. A check posted by an external service
(pre-commit.ci, GitGuardian, Sentry, a self-hosted status reporter) has no
run stored on GitHub's side; the `link` field is the *only* pointer GitHub
has to it; the actual log lives on that service's infrastructure. For these:

- If the link is a public, unauthenticated page, fetch it directly.
- If it requires the service's own login (GitGuardian's dashboard, most
  Sentry links), you generally can't fetch it — work from the check's
  `description` field (often has a one-line summary) and ask the user for
  more detail if that's not enough to act on.

## Re-running checks

Re-running only applies to GitHub Actions runs — third-party checks re-run
on whatever trigger the service itself defines (a new push, in most cases;
pre-commit.ci additionally reacts to a PR comment reading `pre-commit.ci
autofix` or `pre-commit.ci run`).

```bash
gh run rerun <run-id> -R mitodl/agent-kit --failed   # only the failed jobs, not the whole run
gh run rerun <run-id> -R mitodl/agent-kit            # the entire run
```

`<run-id>` is the same numeric ID as `checks[].run_id` from
`fetch-checks.sh`'s output, or `--json jobs --jq '.jobs[] | {name,
databaseId}'` on `gh run view <run-id>` if you need a specific job's ID
rather than the whole run's.

## GitGuardian and other secret scanners specifically

A GitGuardian check failing means a pattern matching a credential shape was
found in the diff (or, depending on configuration, in the full history of
commits being pushed). Two things make this different from an ordinary lint
failure, both covered by the "stop and ask" rule in the main skill doc:

1. **A false positive still needs confirmation, not just your judgment call**
   — API-key-shaped test fixtures and placeholder tokens are common causes,
   but the check exists because that call is easy to get wrong quietly.
2. **If it's real, deleting the line from the current diff doesn't fix
   anything** if the secret was ever committed — it's already in git
   history and fetchable by anyone with clone access. The actual fix is
   rotating the credential at its source and, separately, deciding whether
   history needs rewriting — both decisions for the user, not something to
   do unprompted while "addressing feedback."

## CodeQL

CodeQL can show up two ways on the same PR: as a **check** (`workflow:
"CodeQL"`, pass/fail on whether the scan itself completed) and as **code
scanning alerts** (a separate GitHub feature, not part of the check bar at
all — `gh api repos/{owner}/{repo}/code-scanning/alerts` or the PR's
"Files changed" annotations). A passing CodeQL *check* only means the scan
ran without error; it says nothing about whether the scan found anything.
Don't treat a green CodeQL check as "no alerts" — check the alerts list
separately if the PR conversation or review comments reference one.
