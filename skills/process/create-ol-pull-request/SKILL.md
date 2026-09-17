---
name: create-ol-pull-request
description: >
  Create a pull request in the mitodl organization using their standard PR template.
  Use this skill when asked to create a PR, open a pull request, or submit changes
  for review. Guides branch inspection, title/body population, pre-submit
  checks (a claim audit of the title, body, commit messages, and added text
  against live evidence; an independent review of the diff against its
  stated goals and for security issues; a per-commit secret scan), and
  pushing and running gh pr create.
license: BSD-3-Clause
metadata:
  category: process
---

# Create a Pull Request (`/olpr`)

When the user runs `/olpr`, or asks to open a pull request in a repo with a
`mitodl` remote, guide them through creating a PR using the org's standard
pull request template.

## Auto-detection

This skill should activate automatically (without `/olpr`) when:

- The user says "create a PR", "open a pull request", "submit a PR", etc., **and**
- The current repo has a remote URL containing `github.com/mitodl/` (verify with
  `git remote -v`).

## Step 1 — Inspect the branch and diff

Before prompting the user, gather context automatically:

```bash
# Confirm current branch and its upstream
git --no-pager branch --show-current
git --no-pager log --oneline origin/HEAD..HEAD

# Check for an existing open PR on this branch
gh pr view --json url,title,state 2>/dev/null
```

- If an open PR already exists for the branch, share its URL and stop —
  do not create a duplicate.
- If there are no commits ahead of the base, warn the user before proceeding.

## Step 2 — Determine the base branch

Default to the repo's default branch (usually `main`). Override if the user
specifies a different target.

```bash
gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name'
```

## Step 3 — Gather PR metadata

Ask the user for each field in a **single batched question**, rather than
inferring the body and asking them to correct it:

| Field | How to obtain |
|-------|---------------|
| **Title** | Ask the user; offer one derived from the branch name / commits as a default they can overwrite |
| **Linked tickets** | Ask for issue numbers (Closes #, Fixes #, or N/A) |
| **Description** | Ask what the PR does; summarise from commits only if the user says "summarise"; summary should be short and high level; put detailed technical explanation of the PR in the `<details>` block |
| **Screenshots** | Ask if UI changes are present; skip section if not applicable |
| **Testing notes** | Ask how the changes were tested and how a reviewer can validate |
| **Additional context** | Ask for reviewer notes, caveats, or checklist items |
| **Draft?** | Ask if this should be a draft PR (default: no) |

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

## Step 4 — Populate the template

Fill in the standard PR template below with the gathered information.
Strip HTML comments before passing to `gh pr create`. Show the finished body
and confirm before creating the PR.

```markdown
### What are the relevant tickets?
<!-- Closes #<n> | Fixes #<n> | N/A -->

### Description (What does it do?)
<!-- description -->

<details>
<summary><b>Implementation details</b></summary>
<br>

<!-- agent notes on implementation technical details, ask user if they want to omit this -->
</details>

### Screenshots (if appropriate):
<!-- screenshot checklist, or delete section if not applicable -->

### How can this be tested?
<!-- testing instructions -->

### Additional Context
<!-- notes from author to reviewer -->
```

Checklist section (uncomment and populate **only** if there are pre-merge steps):

```markdown
### Checklist:
- [ ] <step>
```

## Step 5 — Pre-submit checks

Run all four parts, in this order, every time. Each part says when it may
be skipped; nothing else skips it. The order matters: claim fixes can
change code, the review has to see the final code, and the secret scan has
to see every commit, including the ones earlier parts create.

### 5a — Audit claims

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
| Comment, docstring, or Markdown in the diff | Edit the file and commit; 5b reviews it |
| Unpushed commit message | Reword it, with the user's OK since that rewrites history |
| Already-pushed commit message | Correct it in the PR body; don't force-push unless the user asks |

### 5b — Independent review

Run the [`code-review`](../code-review/SKILL.md) skill on the branch
against the base branch from Step 2, with numbered goals passed in. Goal
alignment and security are why this part exists: those are the gaps a
Copilot or human reviewer otherwise finds after the PR is public.

**Goals** come only from sources the authoring session didn't write: the
linked tickets from Step 3 as fully qualified refs (`mitodl/hq#123`, not
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

Act on the report:

- **Confirmed correctness, goal-alignment, or security finding** — fix,
  commit, and re-run the review once. If the second run still reports
  findings, show them to the user instead of looping. If a finding's goal
  came from issue text rather than the user's words, confirm the
  requirement with the user before implementing it, since anyone who can
  edit the issue wrote it. A goal left out on purpose goes in the PR
  description.
- **Simplification, efficiency, reuse, or uncertain finding** — fix it or
  tell the user why not. It doesn't block.
- **A finding you disagree with** — show it to the user with the evidence.

Skip 5b only when the diff touches nothing but prose documentation that no
tool runs and no agent follows, and say so. These never qualify:

- A rename of a setting, env var, config key, or public identifier.
- Agent instructions (skills, agent definitions, prompts, `AGENTS.md`),
  which are the behavior in repos that ship them.
- Comments tooling acts on: `# nosec`, `# noqa`, `# type: ignore`,
  `# pragma: allowlist secret`, `gitleaks:allow`, and similar.
- Dependency bumps, including lockfile-only ones.

### 5c — Scan every commit for secrets

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
If a commit holding it was already pushed, the credential is leaked; tell
the user it needs rotating.

### 5d — Show the user what changed

If 5a–5c changed anything, show the user before Step 6: the new commits
(`git show`), the final title and body, and which changes landed after the
last review run. Label those as not independently reviewed; don't present
them as reviewed. The user confirmed a body in Step 4, not code or claims
changed after it. Don't paste findings tables into the PR body.

## Step 6 — Push and create the PR

Push only now. A branch pushed before Step 5 puts unreviewed code,
unaudited claims, and possibly secrets in public. If it was already
pushed, run Step 5 anyway and push corrections as new commits.

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

## Full PR template (reference)

> Source: https://github.com/mitodl/.github/blob/main/.github/pull_request_template.md

```markdown
### What are the relevant tickets?
<!--- If it fixes an open issue, please link to the issue here. -->
<!--- Closes # --->
<!--- Fixes # --->
<!--- N/A --->

### Description (What does it do?)
<!--- Describe your changes in detail -->

### Screenshots (if appropriate):
<!--- optional - delete if empty --->
- [ ] Desktop screenshots
- [ ] Mobile width screenshots

### How can this be tested?
<!---
Please describe in detail how your changes have been tested.
Include details of your testing environment, any set-up required
(e.g. data entry required for validation) and the tests you ran to
see how your change affects other areas of the code, etc.
Please also include instructions for how your reviewer can validate your changes.
--->

### Additional Context
<!--- optional - delete if empty --->
<!--- Please add any reviewer questions, details worth noting, etc. that will help in
assessing this change.  --->


<!--- Uncomment and add steps to be completed before merging this PR if necessary
### Checklist:
- [ ] e.g. Update secret values in Vault before merging
--->
```

---

## Writing style

The PR body exists to get a reviewer oriented in under a minute:

- Lead with what changed and why. No preamble, no closing summary.
- Bullets over paragraphs; one change per bullet.
- Say it once. The description should not restate the title, and the testing
  section should not re-describe the change.
- No filler adjectives ("comprehensive", "robust", "significant"), no emoji.
- Link to the issue, the doc, or the line of code instead of paraphrasing it.
- Drop an optional section rather than filling it with "N/A"-grade prose.

A five-bullet description that a reviewer can act on beats a wall of narrative.
If the diff is self-explanatory, a two-line description is the correct length.

## Tips

- **Summarise from commits**: if the user asks you to write the description,
  run `git --no-pager log --oneline origin/HEAD..HEAD` and synthesise a
  concise summary from the commit messages.
- **Strip comments**: remove all `<!-- ... -->` blocks from the body before
  calling `gh pr create` to keep the PR clean.
- **Screenshots**: only include the Screenshots section when the PR touches UI
  code. Ask the user to attach images after the PR is created if needed.
- **Checklist**: only uncomment and use the Checklist section when there are
  explicit pre-merge steps (e.g. Vault secret updates, migration runs). Leave
  it out otherwise.
- **Technical Details**: this is where a detailed explanation of the technical
  approach should go instead of the "Description" section. The complexity of
  this explanation should be proportional to the complexity and/or risk of the change.
- **Draft PRs**: suggest `--draft` if the branch is a work-in-progress or the
  user mentions it isn't ready for review.

## Self-contained PRs

Pull requests must be **self-contained and self-documenting**. Do not reference local
files, on-device content, or relative paths that would be inaccessible to reviewers.
Instead:

- **Reference GitHub issues** by their full URL (e.g., `https://github.com/mitodl/ol-django/issues/123`)
- **Reference code files** via GitHub URLs, including line numbers for specific
  references (e.g., `https://github.com/mitodl/ol-django/blob/main/apps/course_info/views.py#L45-L52`)
- **Reference documentation** via its public URL (e.g., Django docs, library API docs)
- **Reference designs** via Figma URLs or other publicly accessible sources
- **Include essential context inline** when no URL is available. If you need to
  reference a specific code pattern, configuration, or decision, include the relevant
  snippets or details directly in the PR body.
- **Include test data or fixtures** inline when describing testing procedures rather
  than referencing local files.
- **Include error messages or log output** directly in the PR description rather
  than describing them or referencing temporary files.
- **Attach screenshots** to the PR after creation rather than referencing local image files.

This ensures reviewers can understand and evaluate the changes without needing access
to the author's local environment or file system.
