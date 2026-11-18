---
name: feature-loop
description: >
  Run the feature loop: plan a change, debate the plan with a read-only reviewer
  on a different provider's model, stop for the user's approval, implement, then
  debate the code with a fresh reviewer, and commit locally. Use this skill only
  when the user explicitly asks for the feature loop or types `/feature-loop`,
  never for an ordinary "implement X" request: it sends the plan and the code to
  a second model provider. The reviewer runs through `scripts/reviewer.py`
  (codex, claude or pi CLI, chosen per user in
  `~/.config/feature-loop/reviewers`).
license: BSD-3-Clause
metadata:
  category: workflow
---

# Feature loop

You, the main session, own every step and do the implementation yourself. A
reviewer on another provider's model challenges the plan and then the code.
The request is the user's message that invoked this skill.

**Debate cap:** 5 rounds per debate.
**Stopping:** whenever you stop for the user's input or finish, make the very
last line of your reply exactly one of:
`⏸ FEATURE-LOOP PAUSED: <what you need from the user>` or
`✅ FEATURE-LOOP DONE: <topic>`.
**Working files:** `<dir>` = `${FEATURE_LOOP_DIR:-~/.feature-loop}/<repo>/<topic>/`,
with the repository's name and a short kebab-case topic. Everything this loop
writes for itself goes there, never in the repo: `state.md`, `start.patch`,
`plan.md`, `decisions.md`, the debate logs, prompt files, diffs and the
reviewer's replies. Never commit them.

## The reviewer

`scripts/reviewer.py` is in this skill's directory (the directory this
`SKILL.md` was loaded from); below it is `$SKILL_DIR/scripts/reviewer.py`. Run it
with `python3`. It picks the reviewer from the user's
`~/.config/feature-loop/reviewers` (or `FEATURE_LOOP_REVIEWER`) and runs it
read-only with no MCP servers.

```bash
python3 "$SKILL_DIR/scripts/reviewer.py" start --caller <family> --dir "<dir>" --prompt-file <file>
python3 "$SKILL_DIR/scripts/reviewer.py" resume --session <token> --dir "<dir>" --prompt-file <file>
python3 "$SKILL_DIR/scripts/reviewer.py" show --caller <family>
```

- `--caller` is your own model family in lowercase: `claude` for Claude models,
  `gpt` for OpenAI GPT models, otherwise your family's name (`glm`, `gemini`).
- Write each prompt to a new file made with `mktemp "<dir>/prompt-XXXXXX.md"`,
  never on the command line and never at a fixed path.
- stdout starts with `reviewer: <backend:model>` and
  `reviewer-session: <token>`, then the reply, then `reviewer-reply: <file>`.
  Copy the `reviewer:` line into the debate log. Resume with the latest token.
- The reviewer can only read. A claude reviewer has Read, Grep and Glob but
  cannot run commands, and it can reach only your working directory and
  `<dir>`: name in the prompt the files it should start from, by absolute path,
  and copy anything it needs from elsewhere into `<dir>`. A codex or pi reviewer
  can search and read any file the user can, not only the ones you name; all of
  it goes to that provider.
- Exit codes: `2` config or usage error, `3` the reviewer session is gone,
  `4` an earlier call on that session is still running, `1` anything else. On
  `3`, start a fresh reviewer with the plan and the debate log so far, and note
  in the log that it is a fallback. On `4`, wait a few minutes and retry, or ask
  the user. On `1` or `2`, stop and show the user the message. Never fall back to
  reviewing it yourself, or to a reviewer on your own provider, unless the user
  says so.
- A call takes minutes. If your shell tool can run a command in the background,
  run it that way and wait for it; otherwise use the longest timeout the tool
  allows. If the call is killed anyway, the token is already in
  `<dir>/reviewer-sessions`: resume with it and ask again.
- **In codex:** the sandbox blocks the network and writes outside the
  workspace. Request escalated permissions for every `reviewer.py` call and
  every write under `<dir>`. If escalation is unavailable (approvals are off),
  stop before Phase 0 and say why.

If `show` exits 2, the user has no reviewer configured. Stop and show them the
example lines it prints.

## Rules for every phase

- **Live advisor notes.** If live advisor notes arrive (some platforms run an
  advisor that watches the session), weigh each `concern` or `blocker` on its
  merits as it arrives: fix it, or write one line in `decisions.md` saying why
  you didn't. Silence is not approval. Notes can also arrive alongside the
  user's reply after you stop: deal with each `concern` or `blocker` that came
  with the message before acting on the message, approvals included.
- **What counts as agreement.** Only the reviewer's explicit sign-off on each
  specific point. The reviewer may accept a point it still disagrees with as a
  reasonable tradeoff; that is a recorded dissent, not a withdrawn objection,
  and it closes the point. Advisor notes made during a debate are input, not a
  second vote: never settle a point by citing them. When you give up a point,
  say which argument changed your mind.
- **Debate honestly.** Don't concede just to finish, and don't dig in to win.
  Accept findings that are right, push back with evidence (file:line, a command
  you ran, a contract) on findings that are wrong, and counter-propose where both
  sides have a point. A correct finding can still be not worth fixing: answer
  `DEFER` findings by logging them under `## Deferred` in `decisions.md`, not by
  changing code.
- Follow your instruction files (scope, verification, branch names, no pushes
  or PRs without asking).

## Phase 0: Record the starting point

Before touching anything, write `<dir>/state.md` with: repo path, branch,
starting `HEAD` sha, and `git status --porcelain` output (files that were
already dirty or untracked). Save `git diff HEAD` as `<dir>/start.patch` so
pre-existing edits can be told apart from this run's later. This run owns only
the commits after that `HEAD` and the files it changes itself. If a file was
already dirty and this run needs to edit it, ask before editing.

## Phase 1: Plan

1. Read the relevant code and instructions, including the repo's `CONTEXT.md`
   (or `CONTEXT-MAP.md`) and `docs/adr/` if they exist.
2. If the request leaves product or domain choices open, ask about them in one
   or two rounds: number each question and give your recommended answer. Ask
   only what the user must decide (behavior, scope, domain meaning); make
   implementation choices yourself and list them in the plan for the reviewer to
   challenge. Look up facts yourself instead of asking. Skip this step when the
   request is already specific. If a choice that blocks the plan is still open
   after two rounds, stop and list it; don't plan around a guess. Record the
   answers in `decisions.md` under `## User decisions` (write `None.` if there
   were no questions).
3. If you find a glossary term or ADR-worthy decision the repo docs should
   capture, write the proposed change to `context-proposals.md`. Don't edit
   `CONTEXT.md` or `docs/adr/` unless the user asks.
4. Write `plan.md`: goal, out of scope, the changes file by file, edge cases,
   tests to add or run, risks, and which subtasks (if any) to delegate and why.
5. Deal with any live advisor notes before moving on.

## Phase 2: Plan review and debate

1. Start a reviewer (`reviewer.py start`). The prompt: read `plan.md`,
   `decisions.md`, the code it touches, and the repo's `CONTEXT.md` if it exists
   (list their absolute paths), and report concrete findings (P0/P1/P2), each
   with evidence, a concrete trigger (the input or state, and the path that
   reaches it), a likelihood (`common` / `edge` / `rare`), and a suggested fix.
   Mark a finding `DEFER` only when it is both `rare` and low-impact; it doesn't
   block `AGREE`. Never defer one that can lose or expose data, touch security or
   money, cause an outage, or break a stated requirement, however rare. Treat
   `## User decisions` in `decisions.md` as settled; challenge one only with
   evidence that it's wrong or contradicts the code. Flag terms the plan uses
   differently from `CONTEXT.md`. End with `Verdict: AGREE`,
   `Verdict: CHANGES`, or `Verdict: ACCEPT-WITH-DISSENT` followed by one line per
   point it still disagrees with but accepts as a reasonable tradeoff.
2. Write your answer to each finding (accept / reject with evidence /
   counter-propose) to `debate-plan.md` under `## Round N`, and update `plan.md`
   for everything accepted.
3. Send your answers back to the **same** reviewer (`reviewer.py resume` with
   the latest token). Ask it to say for each point whether it now agrees, and to
   end with the verdict line.
4. Repeat until `Verdict: AGREE` or `Verdict: ACCEPT-WITH-DISSENT` with no
   points open, or 5 rounds. Copy any dissents to `debate-plan.md` under
   `## Dissents`.
5. If 5 rounds pass without agreement, stop and ask the user. List only the open
   points, with each side's argument in two or three lines.

## Phase 3: User approval (always stop here)

Give the user a short summary: the plan's main points, what changed during the
debate, the reviewer's dissents, and any open risks. Link `plan.md`, and
`context-proposals.md` if it has entries to accept or drop. Wait for approval.
If the user asks for changes, update the plan; run another review round only if
the changes are material.

## Phase 4: Implement

1. Create a branch, following your branch naming instructions, if you're not
   already on one for this work.
2. Implement it yourself. With a subagent facility, you may delegate a subtask
   that is self-contained when handing it off clearly helps (it can run in
   parallel, is large and mechanical, or is easy to describe completely): give it
   a complete brief, since it doesn't see this conversation, keep one writer per
   worktree at a time, and check its diff yourself before building on it.
   Otherwise do that subtask yourself too. If your platform lets you pick the
   subagent's model or effort, use a cheaper, lower-effort one for mechanical
   edits and a stronger one for subtle logic. For parallel writers, give each its
   own git worktree and merge their branches into yours before Phase 5.
3. Run only the tests and checks that cover the changed files, and fix what
   fails.

## Phase 5: Code review and debate

Same as Phase 2, with a fresh reviewer (`reviewer.py start`), applied to this
run's changes against `plan.md`. The reviewer cannot run `git`, so:

- Write `git diff <starting HEAD from state.md>` (commits and uncommitted edits
  to tracked files) to a new file made with `mktemp "<dir>/diff-XXXXXX.patch"`.
- List every file this run created (untracked files don't show in that diff),
  by absolute path, and the pre-existing dirty files to ignore. For files that
  were already dirty and that the user approved editing, include
  `<dir>/start.patch` so the reviewer reviews only this run's edits.
- Give the starting sha.

Ask the reviewer to look for correctness bugs, gaps against the plan, security
issues, missing tests, and names or terms that contradict `CONTEXT.md`, with
evidence. Log to `debate-code.md`, dissents included. Implement the agreed
changes, by the same rule as Phase 4, and rerun the covering tests. The
reviewer's sign-off should cover the changed code, not just your replies: give
it a fresh diff file each round. Cap 5 rounds; if they pass without agreement,
ask the user as in Phase 2.

## Phase 6: Done

Commit by explicit path, staging only files this run changed (no `git add -A`).
Never stage a file that was dirty at the start unless the user approved editing
it, and then only the hunks this run made (`git add -p`). Don't push or open a
PR. Then summarize: branch and commits, files changed, tests run with results,
the rounds in each debate, the reviewer (`backend:model`) and its dissents,
points the user decided, and anything deferred.
