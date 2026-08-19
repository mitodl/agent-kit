---
name: create-ol-pull-request
description: >
  Create a pull request in the mitodl organization using their standard PR template.
  Use this skill when asked to create a PR, open a pull request, or submit changes
  for review. Guides branch inspection, title/body population, a pre-submit audit
  of factual/behavioral claims in the PR body against live evidence, and
  gh pr create.
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
| **Description** | Ask what the PR does; summarise from commits only if the user says "summarise" |
| **Screenshots** | Ask if UI changes are present; skip section if not applicable |
| **Testing notes** | Ask how the changes were tested and how a reviewer can validate |
| **Additional context** | Ask for reviewer notes, caveats, or checklist items; skip if none |
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
<description>

### Screenshots (if appropriate):
<screenshot checklist, or delete section if not applicable>

### How can this be tested?
<testing instructions>

### Additional Context
<reviewer notes, or delete section if not applicable>
```

Checklist section (uncomment and populate **only** if there are pre-merge steps):

```markdown
### Checklist:
- [ ] <step>
```

## Step 5 — Audit factual claims

Before creating the PR, re-read the drafted body for factual or behavioral
claims — anything an "evidence" question would apply to: "prod never showed
this", "this fixes the leak", "the library defaults to X", "this improved
latency", a specific number or timestamp. A change that's purely mechanical
(a rename, a dependency bump with no behavioral claim, a two-line
self-evident diff) has nothing to audit — skip this step rather than
padding the body with an audit table it doesn't need.

When there are claims to check, verify each one against its strongest
available evidence rather than memory or "it should be fine":

| Claim type | Evidence source |
|------------|------------------|
| Metric / production behavior | Prometheus/Grafana via the matching `toolhive-swe-{ci,qa,prod}` MCP tier, over a window of **at least 7 days** so a short blip doesn't read as a trend |
| Library/framework default behavior | The actual library source or its docs — not memory |
| Infra/config state ("this is deployed", "the value is X in prod") | The deployed state, not the manifest — see the `deploy-verification` skill if the claim is about a live rollout |
| "This fixes bug X" | A test that failed before the fix and passes after, if one exists or is cheap to add |

Mark each claim VERIFIED, UNVERIFIABLE, or CONTRADICTED. Rewrite the body
before moving on: drop UNVERIFIABLE claims rather than shipping them
hedged, and correct — don't soften — anything CONTRADICTED. A claim that
can't be checked before the PR opens doesn't get to ship as fact and get
walked back after a reviewer catches it.

## Step 6 — Create the PR

```bash
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
