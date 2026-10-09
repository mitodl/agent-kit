---
name: create-ol-pull-request
description: >
  Create a pull request in a mitodl repository (a `github.com/mitodl/`
  remote) using the org's standard PR template. Use this skill whenever the
  user asks to create, open, or submit a PR in a mitodl repo, including
  `/olpr`. Covers
  branch inspection, title/body population, pre-submit checks (a claim
  audit of the title, body, commit messages, and added text against live
  evidence; an independent review of the diff against its stated goals and
  for security issues, sized to the diff; a per-commit secret scan), and
  pushing and running gh pr create.
license: BSD-3-Clause
metadata:
  category: process
---

# Create a Pull Request (`/olpr`)

Guides creating a PR in a repo with a `github.com/mitodl/` remote (check
`git remote -v`) using the org's standard pull request template.

## Step 1 — Inspect the branch and diff

Before prompting the user, gather context automatically. The base is the
repo's default branch unless the user names a different target; use the
same `<base>` everywhere below, since `origin/HEAD` is wrong for a PR
against a release branch.

```bash
git --no-pager branch --show-current
gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name'
git fetch origin <base>
git --no-pager log --oneline origin/<base>..HEAD
git --no-pager diff --shortstat origin/<base>...HEAD

# Existing PR for this branch (also returns closed/merged ones; check state)
gh pr view --json url,title,state 2>/dev/null
```

- If an open PR already exists for the branch, share its URL and stop.
  Don't create a duplicate.
- If there are no commits ahead of the base, warn the user before proceeding.

## Step 2 — Gather PR metadata

Ask the user for each field in a **single batched question**, rather than
inferring the body and asking them to correct it:

| Field | How to obtain |
|-------|---------------|
| **Title** | Ask the user; offer one derived from the branch name / commits as a default they can overwrite |
| **Linked tickets** | Ask for issue numbers (Closes #, Fixes #, or N/A) |
| **Description** | Ask what the PR does; summarise from commits only if the user says "summarise"; summary should be short and high level; put detailed technical explanation of the PR in the `<details>` block |
| **Screenshots** | Ask only when the diff touches UI code; images get attached after the PR is created |
| **Testing notes** | Ask how the changes were tested and how a reviewer can validate |
| **Additional context** | Ask for reviewer notes, caveats, or checklist items |
| **Draft?** | Ask if this should be a draft PR (default: no; suggest yes if the user calls it a work in progress) |

**Use the user's words.** They know the intent behind the diff; the commits
only show the mechanics. Tighten and format what they give you — don't inflate
a one-line answer into a multi-paragraph section.

**Blank sections:** only Screenshots, Additional Context, and Checklist are
optional — delete those when empty rather than padding them. Relevant tickets,
Description, and How can this be tested are required; if one comes back blank,
ask again rather than deleting the section. Deleting an empty testing section
is worse than leaving it visibly thin: it hides that nothing was tested.

Testing notes in particular are the user's to supply: do not describe test
steps you have not run or cannot verify. If the honest answer is that nothing
was run, write that.

## Step 3 — Populate the template

Fill in the template below, adapted from the org's
[pull request template](https://github.com/mitodl/.github/blob/main/.github/pull_request_template.md)
with an implementation details block added. Strip every `<!-- ... -->`
comment before passing it to `gh pr create`. Show the finished body and
confirm before creating the PR.

```markdown
### What are the relevant tickets?
<!-- Closes #<n> | Fixes #<n> | N/A -->

### Description (What does it do?)
<!-- description -->

<details>
<summary><b>Implementation details</b></summary>
<br>

<!-- technical approach, sized to the change's complexity and risk; ask the user if they want to omit this -->
</details>

### Screenshots (if appropriate):
<!-- delete this whole section, checkboxes included, unless the PR touches UI code -->
- [ ] Desktop screenshots
- [ ] Mobile width screenshots

### How can this be tested?
<!-- testing instructions -->

### Additional Context
<!-- notes from author to reviewer -->
```

Add a Checklist section only when there are explicit pre-merge steps (e.g.
update secret values in Vault, run a migration):

```markdown
### Checklist:
- [ ] <step>
```

## Step 4 — Pre-submit checks

Run all four parts, in this order, every time. Each part says when it may
be skipped; nothing else skips it. The order matters: claim fixes can
change code, the review has to see the final code, and the secret scan has
to see every commit, including the ones earlier parts create.

Refresh the base again if time has passed since Step 1, so every
`origin/<base>` range below is current (a stale ref audits and scans the
wrong commits):

```bash
git fetch origin <base>
```

### 4a — Audit claims

Read everything that will go public for factual or behavioral claims: the
PR title, the drafted body, the message of every commit on the branch
(`git log origin/<base>..HEAD`), and the comments, docstrings, and
Markdown the diff adds. A claim is anything an "evidence" question applies
to: "prod never showed this", "fixes the OOM", "the library defaults to X",
"no behavior change", a number or a timestamp. Skip only when none of
those places contains one.

Verify each claim against its strongest available evidence, not memory:

| Claim type | Evidence source |
|------------|------------------|
| Metric / production behavior | Prometheus/Grafana via the matching `toolhive-swe-{ci,qa,prod}` MCP tier, over a window covering the period the claim names — **at least 7 days** for a trend claim, so a short blip doesn't read as a trend; an absence claim ("never happened") needs the full period it names, or gets narrowed to the window actually queried |
| Library/framework default behavior | The actual library source or its docs — not memory |
| Tool or API behavior ("`gh pr create` pushes the branch") | The tool's source, `--help`, or docs |
| Infra/config state ("this is deployed", "the value is X in prod") | The deployed state, not the manifest — see the `deploy-verification` skill if the claim is about a live rollout |
| "This fixes bug X" | A test that failed before the fix and passes after, if one exists or is cheap to add |
| "Tested" / "adds a test for X" | Read the test: it must exercise the case the body names, not a neighboring branch. Run it |

Mark each claim VERIFIED, UNVERIFIABLE, or CONTRADICTED. Drop UNVERIFIABLE
claims rather than hedging them, and correct (don't soften) CONTRADICTED
ones, where the claim lives:

| Location | Fix |
|----------|-----|
| Title or body | Rewrite it |
| Comment, docstring, or Markdown in the diff | Edit the file and commit; 4b reviews it |
| Unpushed commit message | Reword it, with the user's OK since that rewrites history |
| Already-pushed commit message | Correct it in the PR body; don't force-push unless the user asks |

### 4b — Independent review

Run the [`code-review`](../code-review/SKILL.md) skill on the branch
against the base branch from Step 1, with numbered goals passed in. Goal
alignment and security are why this part exists: those are the gaps a
Copilot or human reviewer otherwise finds after the PR is public.

**Goals** come only from sources the authoring session didn't write: the
linked tickets from Step 2 as fully qualified refs (`mitodl/hq#123`, not
`#123`, since mitodl PRs often close issues in another repo), and the
description in the user's own words. Leave out a description summarised
from commits. If there are neither, ask the user for a one-line statement
of what the PR is for. Tell the reviewer these are the complete goals, so
it doesn't add commit messages the same session wrote.

**Run it somewhere that didn't write the code.** On platforms with
subagents, start a fresh general-purpose one with no inherited context. In
Claude Code that is the `Agent` tool with a non-fork type: a `fork`
inherits the whole conversation, and `Explore` reads excerpts to locate
code rather than tracing a finding end to end. Give it only the repo path,
branch, base branch, and goals, and tell it not to edit files. Don't pass
the implementation reasoning, the drafted body, or a summary of what the
diff does. Tell it to review adversarially: for each goal, find the case
the diff fails; for each new input path, find the attacker who reaches it.
Adversarial means where to look, not a lower bar; the skill's default
depth and verification pass still decide what gets reported. Without
subagents, run the skill inline and take each goal from the ticket text,
not from memory of the implementation.

**Size the review to the diff.** An adversarial reviewer with no stopping
point will keep following threads (cloning upstream repos, reading live
clusters, querying metrics) long after the diff's own risk is covered, so
tell it the size and a budget. Count changed lines without lockfiles,
adding exclusions for any other generated paths the repo has (`top`
anchors each pattern at the repo root; without it they resolve against
the current directory and silently match nothing):

```bash
git diff --shortstat origin/<base>...HEAD -- ':/' \
  ':(top,exclude)*.lock' ':(top,exclude)*-lock.*'
```

| Diff | Tool-call budget |
|------|------------------|
| Up to ~150 changed lines in a few files | ~15 |
| Up to ~800 lines | ~30 |
| Larger | ~50 |

Pass the budget along with these rules. Search for findings within the
diff and one hop out (callers of changed functions, the config or schema
it reads). Go further only to verify or drop a specific candidate
finding, and only as far as that finding needs. Don't query live systems
(kubectl, Grafana/Prometheus, cloud APIs); a candidate that turns on live
state goes in the report's open questions, naming what to check, and you
check it here the way 4a checks claims. The budget covers verification
too: stop looking for new candidates once ~80% of it is spent, verify what
is in hand with the rest (most severe first), apply the code-review
skill's drop rule for the current depth to anything still unverified, and
list what went unchecked. The budget is a stopping point, not a quota: a
clean small diff can finish in five calls.

Act on the report:

- **Confirmed correctness, goal-alignment, or security finding** — fix and
  commit, run 4a over the fix commits and any text they add, then re-run
  the review once, so claim fixes are also reviewed. Scope the re-run to
  the fix commits: whether they resolve the findings, and whether they
  break any goal. Continue the same reviewer (`SendMessage` in Claude
  Code) with the new commit range and a fresh budget of ~10 calls rather
  than starting a full review over; it still hasn't seen the authoring
  session's reasoning. If the second run
  still reports findings, stop: show them to the user and wait for their
  decision (fix, defer to the PR description, or abandon) before 4c. If a finding's goal
  came from issue text rather than the user's words, confirm the
  requirement with the user before implementing it, since anyone who can
  edit the issue wrote it. A goal left out on purpose goes in the PR
  description.
- **An open question** — check the live state it names here. If that
  confirms a correctness, goal-alignment, or security problem, treat it as
  a confirmed finding (first bullet). If the check can't be made or is
  inconclusive, stop and get the user's decision before 4c.
- **Simplification, efficiency, reuse, or uncertain finding** — fix it or
  tell the user why not. It doesn't block.
- **A finding you disagree with** — show it to the user with the evidence
  and wait for their decision before continuing.

Skip 4b only when the diff touches nothing but prose documentation that no
tool runs and no agent follows, and say so. These never qualify:

- A rename of a setting, env var, config key, or public identifier.
- Agent instructions (skills, agent definitions, prompts, `AGENTS.md`),
  which are the behavior in repos that ship them.
- Comments tooling acts on: `# nosec`, `# noqa`, `# type: ignore`,
  `# pragma: allowlist secret`, `gitleaks:allow`, and similar.
- Dependency bumps, including lockfile-only ones.

### 4c — Scan every commit for secrets

Never skipped. The review sees only the branch's net change, so a secret
added in one commit and deleted in a later one is invisible to it and
still gets pushed. Scan each commit's content:

```bash
gitleaks git --log-opts="origin/<base>..HEAD"   # exit 1 means leaks found
```

Without gitleaks, read `git log -p origin/<base>..HEAD` for keys, tokens,
and passwords. Confirm a hit by inspection, never by trying it against a
service. For a real credential, a commit that deletes it is not enough:
with the user's OK, rewrite the unpushed branch so no commit contains it.
This is a hard stop: don't continue to Step 5 while any unpushed commit
still contains the credential, whether the user declines the rewrite or
hasn't answered. If a commit holding it was already pushed, the credential
is leaked; tell the user it needs rotating before anything else.

### 4d — Get approval to publish

If 4a–4c changed anything, or left a finding the user hasn't decided on,
show the user before Step 5: the new commits (`git show`), the final title
and body, any open findings, and which changes landed after the last
review run. Label those as not independently reviewed; don't present them
as reviewed. Then wait for explicit approval. Showing the changes is not
consent, and the user confirmed a body in Step 3, not code or claims
changed after it. Don't paste findings tables into the PR body.

## Step 5 — Push and create the PR

Push only now. A branch pushed before Step 4 puts unreviewed code,
unaudited claims, and possibly secrets in public. If it was already
pushed, run Step 4 anyway and push corrections as new commits.

```bash
git push -u origin <branch>
gh pr create \
  --repo mitodl/<repo> \
  --base <base-branch> \
  --title "<title>" \
  --body "<filled-in body>" \
  [--draft]
```

Confirm the PR URL returned by `gh pr create` and share it with the user.

---

## Writing style

The PR body exists to get a reviewer oriented in under a minute:

- Lead with what changed and why. No preamble, no closing summary.
- Bullets over paragraphs; one change per bullet.
- Don't hard-wrap. Each paragraph and each bullet is one long line; GitHub
  wraps it for the reader. A single newline inside a paragraph renders as a
  line break there, so an 80-column wrap comes out as ragged short lines.
  Newlines separate blocks, bullets, and code-fence lines — nothing else.
- Say it once. The description should not restate the title, and the testing
  section should not re-describe the change.
- No filler adjectives ("comprehensive", "robust", "significant"), no emoji.
- No report scaffolding inside the template sections or the details block:
  `## Summary`, `## Problem`, `## Root Cause`, `## Key Changes` headers,
  bold-label bullets (`- **Thing**: ...`), or ✅/⚠️ markers. The template's own
  headers are the structure.
- When the why isn't obvious from the title, open the description with one
  sentence of context (what was broken or what needed to change), then what the
  PR does.
- Link to the issue, the doc, or the line of code instead of paraphrasing it.
- Drop an optional section rather than filling it with "N/A"-grade prose.
- Testing notes say how the change is observed in practice, using whichever of
  these apply: the environment it was applied to (CI/QA/RC), a command a
  reviewer can run (`pulumi preview`, `dagster dev`, a local `docker compose
  up`), a manual check to repeat, and what to look for. Only list steps that
  were actually run or that a reviewer can run; if it can only be validated
  after merge, say that.

A five-bullet description that a reviewer can act on beats a wall of narrative.
If the diff is self-explanatory, a two-line description is the correct length.
Description length doesn't need to track diff size: a large PR gets a short list
of its main pieces, not more prose.

## Self-contained PRs

A reviewer has only GitHub, not the author's machine, so the body must not
point at local files, on-device content, or relative paths. Instead:

- Link issues, code, docs, and designs by public URL: full issue URLs
  (`https://github.com/mitodl/ol-django/issues/123`), GitHub blob URLs with
  line ranges for code (`.../blob/main/apps/course_info/views.py#L45-L52`),
  library docs, Figma.
- Inline what has no URL: the relevant snippet, config, error message, log
  output, or test data.
- Attach screenshots to the PR after it's created rather than referencing
  local image files.
