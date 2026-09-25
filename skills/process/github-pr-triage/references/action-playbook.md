# Action Playbook

Command-level detail for Phase 5 of the main skill. Every action here is
visible to others (comments, review requests) or hard to reverse (merges) —
get explicit confirmation on the specific PR(s) before running any of these,
even if the user approved "act on the ready ones" in general terms up front.
Show the list, then act.

---

## `needs_first_pass_review` — kick off a first pass

`scripts/request-review.sh` is the single entry point for this bucket. It
re-checks the PR itself before doing anything (via `pr-detail.sh`) and refuses
to act on a draft, or on a PR that already has a submitted review or a pending
requested reviewer — so it's safe to run over an entire
`needs_first_pass_review` batch without re-pinging PRs that moved on between
classification and action:

```bash
./skills/process/github-pr-triage/scripts/request-review.sh mitodl/agent-kit 90            # both bots (default)
./skills/process/github-pr-triage/scripts/request-review.sh mitodl/agent-kit 90 copilot     # Copilot only
./skills/process/github-pr-triage/scripts/request-review.sh mitodl/agent-kit 90 claude      # Claude only
./skills/process/github-pr-triage/scripts/request-review.sh mitodl/agent-kit 90 --force     # override the safety guard
```

**Copilot mode** calls `POST /repos/{owner}/{repo}/pulls/{number}/requested_reviewers`
with `reviewers[]=copilot-pull-request-reviewer[bot]`. Requires the repo to
have Copilot code review enabled (org/repo setting) — a failure here usually
means it isn't. Copilot then posts its review as a normal `COMMENTED`-state
review within a minute or two; the script does not block waiting for it.

**Claude mode** posts a PR comment (default body: `"@claude review this PR"`,
override with `--claude-trigger "..."`) rather than calling a reviewer-request
API — there is no Copilot-style "add Claude as a requested reviewer"
endpoint. This only produces an actual review if the target repo has a Claude
GitHub Action installed and configured to react to PR comments (e.g.
`anthropics/claude-code-action` on the `issue_comment` event). **As of this
writing, no mitodl repo has that workflow installed** (checked via
`gh search code "claude-code-action" --owner mitodl`) — so in this org, Claude
mode currently posts a comment nobody/nothing responds to. Prefer an
in-session first pass instead, which has no dependency on repo configuration.
Re-check for an installed Claude Action before relying on comment-trigger
mode — if the user's org adds one later, this script needs no changes.

### In-session first pass

Pick the first of these the platform supports:

1. **Portable (any platform, including stock Pi):** the
   [`code-review`](../../code-review/SKILL.md) skill, pointed at the PR.
   Its scope input takes a **PR number**, not a URL, and resolves the diff
   with `gh pr diff <number>` from the current repo — so run it from a local
   checkout of the PR's repo (`cd` there first; for a PR in another repo with
   no checkout, clone it or use option 2). Ask for e.g. "code-review PR #90";
   it also pulls the PR body with `gh pr view <number> --json body` as the
   stated goals. It is report-only by default.
2. **Direct, no skill or checkout:** read the PR yourself with
   `gh pr view <number> -R <repo> --json title,body,files` and
   `gh pr diff <number> -R <repo>`, and review the diff against the PR body.
3. **Claude-specific, only where it exists:** Claude Code's built-in
   `/review <pr-url>` command. It is not part of agent-kit and not available
   on Pi or other platforms. (Don't confuse it with a `/code-review` command
   that reviews your *working* diff rather than the PR.)

All three produce a report in the session. Posting findings to the PR —
`gh pr review <number> -R <repo> --comment --body-file <file>` — is a visible
action: show the user the text and get confirmation first, same as every other
action in this playbook.

Ask which bot(s) the user wants if unclear (they said "usually copilot, but
maybe claude"); default to `all` when they haven't expressed a preference.

Do **not** request a human reviewer on the user's behalf unless they name one —
that's a social action with more weight than a bot request.

---

## `approved_ready_to_merge` — merge

```bash
./skills/process/github-pr-triage/scripts/merge-pr.sh mitodl/ol-infrastructure 4902 squash
```

Merge method defaults to `squash` if omitted. Before calling it:

1. Confirm with the user which specific PR(s) — paste the list, don't assume
   "all of them" even if they said "merge the ready ones". A short recap
   ("these 3: #4902, #4924, #92 — go ahead?") is enough; don't require a
   PR-by-PR round trip for a small, already-shown batch.
2. Check whether the repo has a house merge-method convention (squash is by
   far the most common default across mitodl repos, but check
   `gh repo view <repo> --json squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed`
   if unsure, or just ask).
3. Re-verify `mergeable`/checks are still green right before merging if any
   time has passed since classification — CI can go red between the report and
   the act phase.

`merge-pr.sh` always passes `--delete-branch`. If the user wants the branch
kept (e.g. it's used elsewhere), merge manually with `gh pr merge` instead of
the script.

---

## `changes_requested` / `has_review_comments` (not `feedback_likely_addressed`)

This is the bucket that needs real judgment, not just a script call:

1. Pull the full thread for context, not just the summary already in the
   classified JSON:
   ```bash
   gh pr view <number> -R <repo> --json latestReviews,comments,reviews \
     --jq '.latestReviews[] | {author: .author.login, state, body}'
   ```
2. Read what's actually being asked. Bot reviews (Copilot, Gemini, etc.) often
   bundle several findings in one comment body — treat each as a separate
   item, not a single ask.
3. If a code fix is needed, work from the actual local checkout for that repo
   (not the tracker/triage context), in a separate git worktree on the PR's
   branch (commands below) rather than switching branches or stashing in the
   checkout itself — fix, run the repo's own tests/lint, commit, push.
4. After pushing, comment summarizing what changed (mirrors the pattern
   already used on PR threads in this org — see e.g. how prior "Addressed in
   `<sha>`: ..." comments are written) and consider re-requesting review from
   whoever left the original feedback.

**Multiple PRs across different repos need fixes** — the shape that suits
parallel delegation, when the platform has it:

- **With a subagent facility** (Claude Code's `Agent` tool, or on Pi an
  installed extension such as `pi-subagents`; Pi core has none): one subagent
  per repo/PR pair, each briefed with that PR's specific review comments, the
  path of a **dedicated git worktree** for that PR's branch, and the
  instruction to fix, test, and push (not to merge or close anything).
- **Otherwise, sequentially:** handle the same repo/PR pairs one at a time
  yourself, with the same brief per PR, finishing (fix, test, push, comment)
  each before starting the next.

Either way, give each PR its own worktree rather than switching branches or
stashing in a shared checkout:

```bash
branch=$(gh pr view <number> -R <repo> --json headRefName --jq .headRefName)
git -C <repo-checkout> fetch origin "$branch"
git -C <repo-checkout> worktree add "../<repo>-pr-<number>" "$branch"
```

`worktree add` with a branch name that exists only as `origin/<branch>` creates
a local tracking branch for it; if a local branch of that name already
existed, it is used as-is, so run `git pull --ff-only` inside the new worktree
before editing. If it refuses because the branch is already
checked out in another worktree, work in that worktree instead of forcing it.
This assumes the PR's branch lives in the base repo, as `@me` PRs in mitodl do;
for a fork PR, fetch from the fork's remote instead.

Parallel subagents sharing one checkout would trample each other's branch, and
even sequentially a `git stash` / branch switch in the user's checkout risks
mixing their uncommitted work into a fix. Remove each worktree
(`git worktree remove`) once its push lands. Even where a subagent facility
is available, don't fan out subagents for a single PR or a single repo —
that's just sequential work wearing a parallel costume.

**Don't auto-trust `feedback_likely_addressed: true`** as a reason to skip a
PR entirely in the report — still list it, just flag it as lower priority to
re-read. It's a timestamp heuristic (newest comment postdates newest review),
not a content match; it can't tell whether the response actually resolved the
concern, and it's blind to fixes pushed without an accompanying comment.

---

## `awaiting_review` — usually nothing to do

By default, report only. If the user wants to nudge stale ones, ask for (or
infer) a staleness threshold, then post a short, non-pushy comment — don't
re-request review (that can reset review state / notify people again
unnecessarily) unless they specifically ask for that.

---

## `approved_blocked` — triage by `blocked_reason`

| `blocked_reason` | What to do |
|---|---|
| `unknown_pending_recompute` | Re-run `scripts/pr-detail.sh <repo> <number>` after a few seconds; GitHub just hasn't finished computing mergeability. Often resolves to `approved_ready_to_merge` on the next check with no other action needed. |
| `merge_conflict` | Needs a rebase/merge from base — this is code work, handle like the feedback bucket above (local checkout, resolve, push). Flag to the user rather than silently resolving conflicts in a way they haven't seen. |
| `checks_failing` | Look at the specific failing check via `statusCheckRollup[].detailsUrl` before guessing at a fix — CI failures are too varied to have a generic playbook step. |

---

## `draft` — report only

Never request reviews or merge a draft. If a draft looks stale and the user
wants it flagged, that's a report annotation, not an action.

---

## Common patterns

**Bot-authored PRs (Renovate, Dependabot) don't usually appear here.**
`fetch-prs.sh` defaults to `--author @me`; a bot's own PRs only show up if you
explicitly pass `--author renovate[bot]` or similar. If the user wants those
triaged, that's closer to the `dependency-updates` skill's job than this one —
point them there instead of reinventing it.

**Very large orgs / high PR counts.** `gh search prs` is capped at 200 in
`fetch-prs.sh` and GitHub's search API caps at 1000 results per query
regardless. If the count returned equals the limit, say so — the report may be
truncated, not exhaustive.

**A PR the user doesn't own but is listed as a reviewer on** is a different
query entirely (`--review-requested @me` instead of `--author @me`) — this
skill doesn't cover that mode; say so if asked rather than quietly mixing the
two result sets.
