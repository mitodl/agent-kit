---
name: create-ol-github-issue
description: >
  Create a GitHub issue in the mitodl organization using their standard issue templates.
  Use this skill when asked to create a GitHub issue, open an issue, or file a bug
  report. Guides user through repo selection, template choice, and gh issue create.
license: BSD-3-Clause
metadata:
  category: process
---

# Create a GitHub Issue (`/olissue`)

When the user runs `/olissue`, guide them through creating a GitHub issue in a
`mitodl` repository using the org's standard issue templates.

## Default organization

Always default to **`mitodl`** unless the user explicitly names a different org.

## Step 1 — Gather required inputs

Ask the user for:

| Field | Notes |
|-------|-------|
| **Repository** | e.g. `ol-django` — org is implied as `mitodl` |
| **Issue type** | See template menu below |
| **Title** | Short, imperative sentence |
| **Body details** | Their text for each section of the chosen template |

Ask in a **single batched question** covering everything missing — one round
trip, not a sequence of one-field prompts.

**Ask for the body text; don't invent it.** The user knows what the issue is
about and you don't. Ask for their words per template section and use them
mostly as given — tighten wording and fix formatting, but do not expand a
one-line answer into three paragraphs. Only draft a section yourself when the
user asks you to or points you at source material (an error, a code path, a
thread) — and then show the draft before creating the issue.

If the user leaves a section blank, delete it rather than padding it.

## Step 2 — Choose a template

Present these four options and apply the matching template body:

| # | Name | Labels | Template file |
|---|------|--------|---------------|
| 1 | Bug Report | `bug` | [bug.md](#template-bug-report) |
| 2 | Technical Issue | _(none)_ | [default.md](#template-technical-issue) |
| 3 | Product Issue | _(none)_ | [product.md](#template-product-issue) |
| 4 | Design QA | `design QA` | [designQA.md](#template-design-qa) |

## Step 3 — Create the issue

Show the filled-in body and confirm before creating it. Use the GitHub CLI:

```bash
gh issue create \
  --repo mitodl/<repo> \
  --title "<title>" \
  --body "<filled-in template body>" \
  --label "<label>"   # omit if no label for this template type
```

Confirm the URL returned by `gh issue create` and share it with the user.

---

## Template: Bug Report

Labels: `bug`

```markdown
<!--- Provide a general summary of the issue in the Title above -->

### Expected Behavior
<!--- Explain what should happen -->


### Current Behavior
<!--- Describe what happens instead of the expected behavior -->


### Steps to Reproduce
<!--- Provide a link to a live example, or an unambiguous set of steps to -->
<!--- reproduce this bug. Include code to reproduce, if relevant -->
1.
2.
3.
4.

### Possible Solution
<!--- optional — delete if empty -->
<!--- Do you have any ideas how to fix this bug? -->


### Additional Details
<!--- optional — delete if empty -->
<!--- If there are additional details that are helpful for addressing this bug please add them here -->
```

---

## Template: Technical Issue

Labels: _(none)_

```markdown
### Description/Context
<!-- What needs to be done? What additional details are needed by the person who will do the work? -->


### Plan/Design
<!--- How do you plan to achieve the stated goals? --->
<!--- Include any design documents or visual mockups as relevant --->
```

---

## Template: Product Issue

Labels: _(none)_

```markdown
### User Story
<!-- Why does this need to be done? Who will it benefit and how? -->
- As a ..., I want to ..., so I can ...

### Description/Context
<!-- What needs to be done? What additional details are needed by the person who will do the work? -->


### Acceptance Criteria
<!-- What are the concrete outcomes that need to happen for this to be "done"? -->
- [ ]

### Plan/Design
<!--- How do you plan to achieve the stated goals? --->
<!--- Include any design documents or visual mockups as relevant --->
```

---

## Template: Design QA

Labels: `design QA`

```markdown
<!--- Title template: "Design QA: <Template/Section/Component> -->

### Relevant Links
<!--- Include Figma and/or relevant reference links -->


### Prioritized List of Issues
<!--- Provide a prioritized checklist of design feedback with relevant screenshots and details. Include indication of high, med, low priority from a design perspective -->
1. `high`
2. `high`
3. `med`
4. `low`

### Additional Details
<!--- optional — delete if empty -->
<!--- If there are additional details that are helpful for addressing the design feedback please add them here -->
```

---

## Writing style

The issue is read by a busy person deciding whether to pick it up. Keep it
short and factual:

- Plain sentences. No preamble, no scene-setting, no closing summary.
- Bullets and code blocks over paragraphs. One idea per bullet.
- Say it once. Don't restate the title in the body or repeat a section's
  content in another section.
- No filler adjectives ("comprehensive", "robust", "critical") and no emoji.
- Link rather than explain — a URL beats a paragraph describing what's behind it.
- Paste the actual error, log line, or snippet instead of narrating it.

A three-bullet issue that says exactly what is wrong beats a page of context.

## Tips

- Fill in the template sections with the user's details **before** calling
  `gh issue create`. Populated templates are more useful than placeholder text.
- Strip HTML comments (`<!-- ... -->`) from the final body to keep the issue clean.
- If the user provides a full `org/repo` slug, use it as-is instead of prepending `mitodl/`.
- For Design QA issues, remind the user to follow the title convention:
  `Design QA: <Template/Section/Component>`.

## Self-contained issues

Issues must be **self-contained and self-documenting**. Do not reference local files,
on-device content, or relative paths that would be inaccessible to others. Instead:

- **Reference GitHub issues** by their full URL (e.g., `https://github.com/mitodl/ol-django/issues/123#issuecomment-456`)
- **Reference code files** via GitHub URLs (e.g., `https://github.com/mitodl/ol-django/blob/main/apps/course_info/views.py#L45-L52`)
- **Reference documentation** via its public URL (e.g., Django docs, library docs)
- **Reference designs** via Figma URLs or other publicly accessible sources
- **Include essential context inline** when no URL is available. If you need to reference
  a local log, error message, or specific code snippet, copy and paste it directly into
  the issue body.
- **Include log output or error messages** directly in the issue rather than describing
  them or referencing temporary files.
- **Attach screenshots** to the issue after creation rather than referencing local image files.

This ensures anyone can understand and act on the issue without needing access to the
reporter's local environment or file system.
