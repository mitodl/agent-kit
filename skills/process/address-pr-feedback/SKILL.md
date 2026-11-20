---
name: address-pr-feedback
description: >
  Fetch, categorize, and address GitHub pull request review feedback (inline
  review threads, discussion comments, failing checks such as prek/autofix.ci,
  pre-commit.ci, GitGuardian, CodeQL), paginating through long-running PRs.
  Applies fixes, replies to reviewers with evidence, resolves threads via
  GraphQL, and diagnoses failing checks from their logs. Has a draft-only
  mode that commits locally and shows a reply table but pushes, posts, and
  resolves nothing until approved, and edits pending review comments without
  clobbering the user's own changes. Use this skill when asked to "address PR
  feedback", "address the comments on <PR>", "resolve the review comments",
  "check the Copilot comments", "fix the failing checks", "the CI is red on
  <PR>", "draft replies without posting", "don't post anything", "leave it
  pending", "show me first", or to triage feedback from bots (Copilot,
  Gemini, CodeQL, Sentry), hook autofixers, or human reviewers on a specific
  pull request.
license: BSD-3-Clause
metadata:
  category: process
---

# Address PR Feedback

Turns a PR's review activity — and its CI/status checks — into fixes, replies,
and a green check bar. Review activity arrives on three channels: inline review
threads, top-level discussion comments, and the bodies of the reviews
themselves. Only the first has a resolved/unresolved state, so a thread count is
never the measure of whether a PR has been addressed. The core pattern is
**fetch → categorize → address → reply & resolve → verify**, using scripts
for the mechanical parts so the interesting work is reading feedback
correctly and knowing when a decision belongs to a human instead of you.

| Script | Purpose |
|--------|---------|
| `scripts/fetch-feedback.sh` | Fetch review threads (paginated), discussion comments, and reviews in one JSON payload |
| `scripts/fetch-checks.sh` | Fetch status checks (GitHub Actions + third-party) and failed-step logs for any failing Actions run |
| `scripts/resolve-thread.sh` | Reply to (optional) and resolve one review thread by GraphQL node ID |
| `scripts/resolve-threads.sh` | Batch version: reads `[{"thread_id": "...", "comment": "..."}, ...]` from stdin |
| `scripts/reply-comment.sh` | Post a top-level PR comment — for discussion-comment replies or a final summary |

See [references/graphql-reference.md](references/graphql-reference.md) for
the underlying queries/mutations, why plain `first: N` GraphQL calls silently
truncate on long-running PRs, and why replying and resolving are separate
mutations. See [references/checks-reference.md](references/checks-reference.md)
for how the Checks API and third-party status checks differ, and how to
re-run or diagnose each.

## Recognizing the request

The trigger is almost always a short imperative naming a PR, sometimes
bundled with other git operations:

- "Address the PR feedback [on #N / \<url\>]"
- "For any comment that is addressed, mark it as resolved"
- "Address PR feedback, resolving comments that have been handled"
- "Rebase, resolve the conflicts, and then address the PR feedback"
- "Address feedback across the set of open PRs" / "walk through the PR chain,
  addressing feedback on the PRs first, then merge" (a stacked-PR batch)
- "Review the latest human-contributed feedback" / "there's more bot feedback
  to evaluate" (a re-check after previously addressing a first round)
- "Fix the failing checks [on #N]" / "the CI is red, why?" / "address the
  GitGuardian alert" / "prek is failing" / "pre-commit.ci is failing" — a
  checks-only request,
  not review-comment feedback; jump straight to
  [Phase 1b — Checks](#phase-1b--checks) below
- "Address the PR feedback" alone, with no mention of checks, still means
  glance at the check bar (Phase 1b) — a PR with unresolved review comments
  *and* a failing required check is not addressed until both are handled

Resolve two things before running anything, asking if either is unclear:

1. **Which PR(s)?** A URL or `#N` in the message is unambiguous — use it.
   When none is given, use this session as context before asking: if you
   opened or pushed to a PR earlier in this same conversation, that's almost
   always the one meant (dogfooding a skill you just built, continuing work
   you just pushed) — don't re-ask for something you already know. Otherwise
   fall back to `gh pr view --json url,number` for the current branch's
   tracked PR, and only ask the user if that's still ambiguous (no tracked
   PR, or reason to think the branch points at the wrong one). A request
   spanning multiple PRs (a stack, or "all my open PRs") means working
   through each one, one at a time — don't parallelize commits across PRs
   that depend on each other.
2. **Report or act?** This skill is almost always invoked to *act* (fix code,
   reply, resolve) — that's what "address" means here, unlike
   [`github-pr-triage`](../github-pr-triage/SKILL.md) which defaults to
   report-only. If the user says "review the feedback" or "give me a summary"
   without "address"/"fix"/"resolve", treat it as read-only: categorize and
   report, but don't change code or touch thread state. A third answer sits
   between the two: "draft", "don't post", "leave pending", "show me first",
   "dry run", or "nothing goes up until I say" means
   [draft-only mode](#draft-only-mode). Fix locally, but push, post, and
   resolve nothing until the user approves.

## Phase 1 — Fetch

```bash
./skills/process/address-pr-feedback/scripts/fetch-feedback.sh mitodl/agent-kit 116 /tmp/pr116-feedback.json
```

This is one call regardless of PR size — it paginates all three channels
internally (GraphQL cursors for review threads, slurped REST pages for
discussion comments and reviews), so a PR with 200 threads across a dozen
review rounds comes back complete, not truncated at the first page. By
default it excludes already-resolved threads; add `--include-resolved` when
you need the full history (e.g. re-checking a PR you partially addressed in
an earlier session, or producing a final "here's everything, resolved and
not" summary).

Output shape: `{repo, pr, review_threads: [...], discussion_comments: [...],
reviews: [...], pending_review: {...} | null}`. Each `review_threads[]` entry carries the GraphQL `id`
(pass this straight to `resolve-thread.sh`), `isResolved`, `isOutdated`,
`path`/`line`, and its `comments`. Every comment, discussion comment, and
review also carries `author_type` alongside `author` — GitHub's own actor
type (`User`, `Bot`, `Organization`, `Mannequin`), normalized from GraphQL's
`__typename` and REST's `user.type` so it reads the same regardless of which
call produced the record. Classify authorship from that field, not from
whether you recognize the login.

`discussion_comments[]` and `reviews[]` carry the other two channels, each entry
with an `html_url` to link it by. Neither has an `isResolved` field, because
GitHub gives them no resolution state at all — nothing marks one read, replied
to, or handled, so neither can ever appear in a count of outstanding work. That
is exactly why they get missed: the highest-stakes feedback on a PR is often a
colleague's architectural judgment left as a top-level comment, and a bot's
summary review usually carries its real findings in `reviews[].body` rather than
in any thread. Read both in Phase 2, account for both in Phase 5.

Entries on both carry `is_viewer`, true for the ones you wrote — the summary
comments Phase 4 posts land on the same endpoint a reviewer's comment does, and
only this field tells them apart. Skip your own by that field rather than by
recognizing the login in `viewer` (the authenticated account); without it every
pass re-triages the summary the last pass left.

`pending_review` is the viewer's own unsubmitted review (`null` when there
is none), with each comment's node `id` and a snapshot of its `body`. When
it is set, the script also prints "You have a pending review with N
comment(s)" to stderr. Pass that on to the user, and follow
[Editing a pending review comment](#editing-a-pending-review-comment) before
touching any of those comments.

## Phase 1b — Checks

```bash
./skills/process/address-pr-feedback/scripts/fetch-checks.sh mitodl/agent-kit 116 /tmp/pr116-checks.json
```

Covers both GitHub Actions jobs (including the `prek` hook check) and
third-party status checks (pre-commit.ci, GitGuardian, Sentry, CodeQL, and
anything else posting to the PR's check bar) in one call. Excludes passing/skipped checks by default; add
`--include-passing` for the full bar (e.g. a final "everything green" report).

Output shape: `{repo, pr, checks: [...], action_run_logs: {<run_id>:
<log text>}}`. Each `checks[]` entry has `name`, `bucket`
(`pass`/`fail`/`pending`/`skipping`/`cancel`), `state`, `description`,
`workflow`, `link`, and a derived `run_id` when the check is a GitHub
Actions job. For every *failing* check with a `run_id`, `action_run_logs`
holds that run's failed-step output (deduped — several failing jobs from the
same workflow run share one log entry), truncated to the last ~20k
characters, so you can usually diagnose without a separate `gh run view`
call. Checks with `run_id: null` (pre-commit.ci, GitGuardian, Sentry, a
legacy commit-status check, etc.) have no fetchable log here — their `link`
points at the external service; fetch it directly if it's a public page,
otherwise summarize from `description` and ask the user if more detail is
needed — rather than guessing at the failure from the name alone.

A `pending` check isn't a failure — don't "fix" something that's still
running. If everything is `pass`/`pending` and the user asked to address
*checks* specifically, say so and stop; there's nothing to act on yet beyond
noting what's still in flight.

## Phase 2 — Categorize

Group by reviewer and tag each item with a priority, inheriting the
reviewer's own label when it supplies one (bot reviewers like
`gemini-code-assist` and `copilot-pull-request-reviewer` usually embed a
severity in the comment body or as a badge) — otherwise infer from
substance:

```text
1. **gemini-code-assist** (medium): transport type mismatch in agent-config.toml
2. **copilot-pull-request-reviewer** (nitpick): variable naming in fetch loop
3. **human — tmacey** (substantive): reconsider the retry backoff strategy
```

`isOutdated: true` does **not** mean "safe to skip" — the line moved but the
underlying concern may still apply. Read the current code at that path
before deciding a stale-looking thread is moot.

Bot review-state fields (`APPROVED`/`COMMENTED`/`CHANGES_REQUESTED` in
`reviews[]`) tell you formality, not substance — most bots leave everything in
`COMMENTED`, so the state never tells you whether a review carries findings.
Only the body does, and bots differ in where they put them: Copilot and Gemini
leave the body empty and the substance in thread comments, while a summary
reviewer like `agent-optibot` does the opposite.

**Read every discussion comment and every non-empty review body, not just the
threads.** They go in the same priority list as thread items — they are feedback
with no thread attached, not metadata. Skip only the entries with
`is_viewer: true`, which are yours: a summary comment from an earlier round, or
your own review of someone else's PR. The human-vs-bot rule below applies to
everything that's left, decided from the same `author_type` field.

Bot review bodies vary by vendor: `agent-optibot` files findings under a
`**Risks**` heading inside a `<details>` block, others use `Issues`, `Concerns`,
or a severity table, and any of them can change shape without notice. Read the
body as prose rather than pattern-matching one vendor's markup — anything naming
a file, a function, or a behavior and saying something is wrong is a finding,
wherever it sits. An empty body is a review that only carried thread comments;
there is nothing to read there.

**A human reviewer's comment gets more deliberate handling than a bot's,
even when the fix looks equally obvious.** A bot flags a pattern; a human
colleague is making a judgment call, and a quick autonomous fix-and-resolve
can read as brushing off their input. For human-authored threads: don't
auto-resolve on your own judgment alone — bring the proposed fix (or your
reasoning for declining) to the operator before pushing and resolving, and
use their framing in the reply rather than your paraphrase. Bot-authored
threads (Copilot, Gemini, CodeQL, etc.) don't need that same checkpoint —
address, reply, and resolve those directly per the rest of this skill.
In [draft-only mode](#draft-only-mode), bot threads wait for approval too.

Decide which branch applies from `author_type` (Phase 1's output), never
from whether the login *looks* like a bot — an unfamiliar GitHub App or a
newly-added reviewer has no recognizable name, and guessing wrong applies
the wrong handling to a colleague. `Bot` (and `Mannequin`, an imported
placeholder identity) take the bot branch; `Organization` is rare on review
threads and takes the human branch.

`User` is the one value that doesn't settle it: a service account driving
automation through a normal user token reports `User` exactly like a person
does, because GitHub has no field that distinguishes them. When a `User`
thread reads like machine output — templated phrasing, a severity badge, an
identical comment repeated across files — say that's your read and ask,
rather than silently taking the bot branch on a thread a colleague wrote.

Failing checks split into three kinds that get handled differently in
Phase 3 — tag each one on the way in:

1. **Your own CI** (unit/integration tests, lint, build, typecheck — usually
   `workflow` is a repo-owned workflow name). A code problem to fix like any
   other actionable item: read `action_run_logs[<run_id>]` for the actual
   failure, fix it, push, and the check re-runs on its own.
2. **Hook checks with an autofix bot.** In mitodl repositories this is
   usually the `prek` check: a GitHub Actions job in
   `.github/workflows/autofix.yml` (workflow name `autofix.ci`), so its log
   is in `action_run_logs`. When the hooks only made fixable changes, the
   check goes red and `autofix-ci[bot]` pushes one fix commit to the PR
   branch, and the check re-runs green on it. When that commit is coming,
   wait for it and `git pull` before pushing anything else, or your push is
   rejected as non-fast-forward. On a fork PR without "allow edits by
   maintainers", no commit comes: the app comments instead, and the author
   applies the fix locally. The check stays red with no bot commit when a hook
   reports something it can't fix, when the fix touches `.github/`
   (autofix.ci refuses those), or when the fix doesn't apply to the PR head.
   Fix those locally: run the repo's hooks (`uv run prek run --all-files` in
   uv repos, `npx prek run --all-files` in Node repos), commit, and push.
   Repositories not yet migrated use pre-commit.ci instead, which reacts to
   a PR comment (`pre-commit.ci autofix`), or you can run the same hooks
   locally and push the diff. Check the failure's log or `link` for the
   specific instruction before guessing.
3. **Security/compliance scanners** (GitGuardian, CodeQL alerts, Sentry-linked
   checks, secret scanning). Never treat these like an ordinary lint failure
   — see the "Security/compliance-flagged findings" rule in
   [When to stop and ask instead of deciding](#when-to-stop-and-ask-instead-of-deciding).
   This applies even when the check is the *only* thing blocking merge and
   the fix looks obvious (e.g. "just delete the leaked-looking string") —
   deleting a secret from the current diff does not remove it from git
   history, and rotation is a decision with consequences beyond this PR.

## Phase 3 — Address

For each actionable item: make the code change, run the relevant
tests/lint/build for that change before moving to the next one. Commit with
a message that names what was addressed, not just "address PR feedback".

"Relevant" means the tests that cover the files you changed, not the whole
suite:

- pytest: the touched modules' test files (`pytest tests/test_foo.py`), or
  `-k` on the affected names.
- vitest: `vitest related --run <changed files>`.
- jest: `jest --findRelatedTests <changed files>`.
- Lint and hooks: `prek run --files <changed files>` (or the repo's
  equivalent) rather than `--all-files`.

Run the full suite only when the repo's CI-equivalent finishes in a couple
of minutes, or the user asks for it. Every reply that claims a fix names the
tests that were run, so the reviewer can tell a scoped run from a full one.

When the item is a genuine bug (not a style/naming nit), prefer proving the
fix over asserting it: extend or add a test that fails against the unfixed
code, confirm the failure, then apply the fix and confirm it passes. This
before/after result is worth citing in the reply or PR summary — it's
stronger evidence than "fixed" on its own, and it's cheap when the bug is
already localized by the reviewer's comment.

**Disagreeing with a reviewer is a legitimate outcome — but it must be
verified, not assumed.** Before skipping a suggestion because it "looks
unnecessary": read the actual code path the reviewer is pointing at and
confirm your reasoning holds (e.g. a suggested defensive check is redundant
only if you've confirmed the caller genuinely always guarantees the
precondition — check every call site, not just the obvious one). Once
verified, say so explicitly in the reply (with the evidence) — never resolve
a thread by silently ignoring it, and never reply with a content-free "noted"
or "will consider". A good decline reads like: *"Verified — `write_bindings`
always calls `ensure_bridge_store` first at every call site, so this
fallback path is unreachable. Not adding it; would reintroduce the redundant
read this PR removes."*

**The same verify-before-asserting rule applies to any claim about runtime
or production behavior**, not just code-path reasoning — "prod never showed
this", "this is why the metric moved", "the library defaults to X". Code
reading alone doesn't verify those; check the live source instead
(Prometheus/Grafana, the actual library source/docs rather than memory, or
the infra definition as deployed rather than as written). Size the query
window to the claim: an assertion about a *period* — "prod never showed
this", "this has been broken since the July deploy" — is only supported by a
window covering that whole period, and if retention won't reach that far
back, the claim gets narrowed to what you actually queried ("no occurrences
in the last 30 days") rather than asserted whole. Seven days is the floor
for *trend* claims, where the window exists to keep a short blip from
reading as a trend — it is a minimum, not a sufficient window for an
absence claim. A claim you can't
verify before replying gets flagged as unverified or left out — it doesn't
ship as fact and get walked back after a reviewer catches it.

For a failing check, read `action_run_logs[<run_id>]` (from Phase 1b) before
touching code — a stack trace or assertion diff tells you exactly what broke,
where a guess from the check *name* alone often doesn't. If a test looks
flaky rather than broken by this PR's changes (same test fails intermittently
across unrelated commits, or the log shows a timeout/network blip unrelated
to the diff), say that's your read and re-run rather than "fixing" a
non-issue:

```bash
gh run rerun <run-id> -R mitodl/agent-kit --failed
```

Don't reach for `--failed` reruns as a first response to every red check,
though — re-running without understanding the failure just burns CI minutes
and may mask a real, intermittently-reproducing bug. Only use it once you've
read the log and concluded the failure is genuinely unrelated to this PR's
changes.

## Draft-only mode

Some users want to see every reply and fix before anything reaches GitHub,
bot threads included. The request says so: "draft", "don't post", "leave
pending", "show me first", "dry run", "nothing goes up until I say". Once
set, the mode holds for the rest of the session, including later rounds of
feedback on the same PR. The autonomous path above stays the default when
none of those words appear.

In draft-only mode:

- Fetch, check, and categorize as usual (Phases 1, 1b, 2).
- Make fixes as local commits, one per row, verified as in Phase 3, so each
  row can be approved on its own. When one fix genuinely covers several
  rows, give them the same commit in the table and treat them as one unit:
  that commit goes up only when every row sharing it is approved.
- Push nothing, post nothing, resolve nothing, and don't re-run checks. That
  covers `git push`, `resolve-thread.sh`, `resolve-threads.sh`,
  `reply-comment.sh`, `gh run rerun`, the pre-commit.ci autofix comment, and
  any edit to a pending review comment.
- End with one table, one row per thread, discussion comment, review-body
  finding, or failing check:

```text
| Item | Author | Verdict | Proposed reply | Local commit | Evidence |
|------|--------|---------|----------------|--------------|----------|
| [a.py:42](https://github.com/o/r/pull/7#discussion_r1) | copilot-pull-request-reviewer | fix | Fixed in a1b2c3d: transport is now streamable-http (tests/test_config.py, 3 passed). | a1b2c3d | tests/test_config.py: 3 passed |
| [b.py:10](https://github.com/o/r/pull/7#discussion_r2) | gemini-code-assist | disagree | Not adding the guard: ensure_bridge_store runs at every call site. | (none) | callers at x.py:12, y.py:40 |
| [review body](https://github.com/o/r/pull/7#pullrequestreview-5928924500): reference_budgets() ignores `before` | agent-optibot | fix | Fixed in d4e5f6a: reference_budgets now filters on the requested date (tests/test_budgets.py, 6 passed). | d4e5f6a | tests/test_budgets.py: 6 passed |
| [discussion comment](https://github.com/o/r/pull/7#issuecomment-7048965644) | rachellougee | needs you | (your wording) | (none) | proposes moving the panel builders to another repo |
```

An item with no thread has no `path:line` to name it by, so link its
`html_url` and say what it is: the review body's finding in a few words, or
just "discussion comment". Rows for those two channels are the ones most
likely to be missing — check the table has one per item Phase 2 found before
showing it.

Verdict is one of `fix`, `reply only`, `disagree`, or `needs you`. The
proposed reply is the exact text that would be posted, in Phase 4's reply
style, so approving the row approves the words.

When the user approves (all rows, some rows, or rows with edited wording),
carry out only the approved rows.

First confirm where the PR's head lives, since `origin` isn't it on a fork
PR. `gh pr view <pr> --json headRepository,headRefName,isCrossRepository`
names the head repo (`headRepository.nameWithOwner`) and branch; find the
local remote whose URL matches it
(`git remote -v`) and use that remote and branch below. Stop and ask if
none matches.

If every row with a local commit was approved, push the branch to that
remote and branch as usual. Otherwise a plain `git push` would also send the
unapproved commits sitting between the approved ones, so build the approved
subset on its own:

```bash
git fetch <head-remote> <head-branch>
git switch -c approved-push FETCH_HEAD
git cherry-pick <approved sha> <approved sha> ...   # in their original order
```

Phase 3 verified the full draft branch, not this subset. An approved commit
can apply cleanly yet depend on a helper from an unapproved one, so re-run
the scoped tests on `approved-push` before pushing. If they fail because of
a dependency on an unapproved commit, stop and tell the user which commit
the approved fix needs. Then push and put the remaining commits back on
top:

```bash
git push <head-remote> approved-push:<head-branch>
git switch <local-branch>
git rebase approved-push   # drops the already-pushed patches, leaves the unapproved commits on top
git branch -D approved-push
```

Reply and resolve only after the push succeeds. Cherry-picking gives the
approved commits new SHAs, so update the SHAs and the test evidence in the
approved reply text to the pushed commits and the subset's test run before
posting. Then reply and resolve with the usual scripts, `--dry-run` first
for any batch. Rows that weren't approved stay as they are; say which ones
remain and which local commits are still unpushed.

## Phase 4 — Reply & resolve

**Reply style: one or two sentences.** Name the commit, what changed, and the tests that ran, or
the evidence and the conclusion. Nothing else — no thanks-for-the-review
opener, no restating the reviewer's comment back at them, no closing offer to
discuss further, no emoji. A reviewer reading twelve replies wants twelve
facts. Write the reply as one unbroken line: GitHub turns a newline inside a
paragraph into a line break, so a hard-wrapped reply renders as ragged
half-lines in the thread.

```text
Fixed in a1b2c3d: transport is now streamable-http (tests/test_config.py, 3 passed).
Verified — ensure_bridge_store runs at every call site, so this path is unreachable. Not adding the guard.
```

Where the reply carries a judgment the user made (a decline they decided on, a
disagreement with a reviewer, a deferral), post **their** framing rather than
your own paraphrase — ask for the wording if you don't already have it. Those
replies are read as the author's position, not the tool's.

For each thread you touched, reply with what changed (cite the commit) and
resolve in one call. Pipe the reply in rather than passing it as an argument:
a reply naming `a_function` in backticks is command-substituted by your shell
before the script sees it, and the identifier silently vanishes from what gets
posted.

```bash
./skills/process/address-pr-feedback/scripts/resolve-thread.sh \
  --thread-id PRRT_kwDORsi4Ac6PsuPQ \
  --comment-file - <<'REPLY'
Fixed in a1b2c3d: switched transport to streamable-http per the schema (tests/test_config.py, 3 passed).
REPLY
```

`--comment "..."` still works for a reply with no backticks, `$`, or quotes in
it. When in doubt use `--comment-file`; there is no case where it is wrong.

For a batch, build the JSON with `jq --arg` — never by interpolating the reply
text into the jq program — and pipe it once:

```bash
jq -n \
  --arg c1 "$(cat reply-1.md)" \
  --arg c2 "$(cat reply-2.md)" \
  '[
    {thread_id: "PRRT_...", comment: $c1},
    {thread_id: "PRRT_...", comment: $c2}
  ]' | ./skills/process/address-pr-feedback/scripts/resolve-threads.sh
```

`--rawfile c1 reply-1.md` reads the file directly if you'd rather skip the
`$(cat ...)`.

Discussion comments, review-body findings, and checks have no resolution state
— there is no thread to mark resolved, so the reply *is* the record. Post it
with `reply-comment.sh`, in the same style as a thread reply: the commit, what
changed, the tests. One consolidated summary covers a pass that touched
several, but a human's substantive comment gets its own reply rather than a
line buried in a summary.

```bash
./skills/process/address-pr-feedback/scripts/reply-comment.sh mitodl/agent-kit 116 \
  --body-file - <<'SUMMARY'
Addressed all review feedback: 4 threads fixed and resolved, 1 declined (see reply) with reasoning.
SUMMARY
```

For a check, the code fix and push is the resolution — it re-runs and flips
green on its own. For a scanner finding you investigated and declined to act on
(Phase 3's security/compliance rule — only after the user has weighed in),
record the reasoning in that summary comment rather than a per-check reply,
since most check UIs don't support one.

Always dry-run (`--dry-run` on both resolve scripts) first when acting on a
batch you haven't manually reviewed thread-by-thread, and check the printed
list before re-running for real.

### Editing a pending review comment

When `pending_review` is set and the user asks you to change one of its
comments (reword it, add a suggestion block, fix a typo), they may have
edited that comment in the browser since you last read it. GitHub's update
mutation replaces the whole body, so writing back a body built from an
earlier read silently discards their edits.

1. Re-fetch the comment's current body by node ID immediately before
   editing it (the `node(id:)` query in
   [graphql-reference.md](references/graphql-reference.md#pending-reviews)).
   Don't reuse the `body` from `fetch-feedback.sh` output or any earlier
   read.
2. Apply the requested change to that body as a patch: change the part
   asked about and keep everything else byte-for-byte.
3. Write it back with `updatePullRequestReviewComment`, passing the body
   from a file.

Never submit the pending review (`submitPullRequestReview`, or `gh pr
review`). Submitting is the user's action, even when every comment in it is
one you wrote.

## Phase 5 — Verify

Re-run `fetch-feedback.sh` (without `--include-resolved`) and account for all
three channels. The unresolved-thread count is the one number the script can
compute and the one number that cannot tell you whether you're done: a PR with
zero unresolved threads and an unread discussion comment reports as clean, and
that is how a reviewer's "this subsystem probably belongs in another repo" gets
shipped past.

Report every item, one line each:

- **Unresolved review threads** — how many remain, and for each one why
  (deferred pending a human decision, blocked on another PR, a design choice
  you're waiting on).
- **Discussion comments** — every one without `is_viewer`, with its disposition:
  fixed (and where), replied, declined with reasoning, or carried to a follow-up
  issue. "Read it, no action needed" is a disposition; silence is not.
- **Review bodies** — every finding you pulled out of a non-empty
  `reviews[].body` in Phase 2, with the same dispositions.

A channel with nothing in it gets said out loud ("no discussion comments", "no
findings in the review bodies") rather than omitted — an omitted channel and an
unchecked one look identical in the report. Never let an item silently drop off.

If you pushed any commits during Phase 3, also re-run `fetch-checks.sh` —
checks take time to complete, so poll (`gh pr checks <pr> -R <repo> --watch
--fail-fast` or a short retry loop) rather than checking once immediately
after the push and reporting on stale `pending` state. Report the final
bucket for every check you touched, not just the ones that turned green —
a check that's still failing after your fix needs the same "why, and what's
next" treatment as an unresolved review thread.

## When to stop and ask instead of deciding

These are not edge cases to handle gracefully and move on from — they're
signals to pause the batch and get the user's input before continuing:

- **Security/compliance-flagged findings** (CodeQL alerts, Sentry issues,
  GitGuardian secret-detection checks, anything touching auth/secrets).
  Never auto-dismiss one as a false positive, no matter how confident the
  verification looks — surface it with your evidence and ask whether to
  dismiss or leave it open. If the tooling itself blocks the action (some
  environments gate CodeQL alert dismissal behind explicit confirmation),
  that's a signal to ask the user directly, not to route around it. For a
  GitGuardian (or similar secret-scanner) hit specifically: removing the
  string from the current diff is not a fix if the secret already landed in
  a pushed commit — it's still live in git history and, if real, needs
  rotation. Ask before doing either; don't just delete the line and call the
  check addressed.
- **A design decision with more than one reasonable fix** — e.g. a
  reviewer flags a race condition and a cheap guard vs. a more thorough
  lock-based rewrite are both defensible. Present the options with
  tradeoffs; don't silently pick the one that looks safer or less invasive.
- **Feedback that conflicts** — with another reviewer, or with a decision
  already made elsewhere in this PR or PR stack. Surface the conflict rather
  than picking a side unprompted.
- **Scope expansion** — a suggestion that implies work outside this PR (split
  into a separate PR, touch a different service/repo, merge in a dependency
  PR first). Ask before doing it, even if it seems like the "right" call.
  ("Address feedback and consider whether it makes sense to merge PR #3565
  into this work" is the user opting into that judgment call explicitly —
  don't assume it by default elsewhere.)
- **Genuinely ambiguous feedback** — unclear what change is being requested,
  or from whom, or what "this" refers to in a short comment. Ask rather than
  guess at intent; a wrong guess costs more than the question.
- **Anything hard-to-reverse or externally visible beyond replying/resolving**
  — merging, force-pushing, closing the PR — stays out of scope for this
  skill unless explicitly requested in this same ask.

If the user has already said something like *"for any case where a decision
is necessary, ask me instead of guessing"*, treat that as standing for the
rest of the batch, not just the first ambiguous item you hit.

## Environment

Requires `gh` (authenticated, with write access on the target repo — see the
[permission-errors note](references/graphql-reference.md#permission-errors))
and `jq`. Fetching Actions logs (`fetch-checks.sh`, `gh run view --log-failed`)
and re-running jobs (`gh run rerun`) need the `actions:read`/`actions:write`
scopes on top of that — both are included by default for `gh auth login`
against a repo you have write access to, so this is usually already covered.
No dependency on any repo-specific tooling; the scripts work from any
checkout via `owner/repo` + PR number.
