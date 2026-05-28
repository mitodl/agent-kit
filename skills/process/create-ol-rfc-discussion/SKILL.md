---
name: create-ol-rfc-discussion
description: >
  Post a properly structured RFC as a GitHub Discussion in the mitodl/hq
  repository under the RFC category. Use this skill when asked to write or
  post an RFC, create a design proposal, or document an architectural decision
  for team review.
license: BSD-3-Clause
metadata:
  category: process
---

# Create an RFC Discussion (`/olrfc`)

When the user runs `/olrfc`, or asks to post/create an RFC, guide them through
writing a well-structured RFC and posting it to `mitodl/hq` as a GitHub
Discussion in the **RFC** category.

## Default target

Always post to **`mitodl/hq`** under the **RFC** discussion category unless the
user explicitly names a different repository.

Known constant IDs for `mitodl/hq` (hardcoded in the posting script —
no lookup needed on the happy path):

| Name | ID |
|------|----|
| Repository | `R_kgDOHOGzLg` |
| RFC category | `DIC_kwDOHOGzLs4COw0u` |

If you ever need to re-derive these (e.g. the repo was renamed or transferred):

```bash
# Repository ID
gh api graphql -f query='{ repository(owner:"mitodl", name:"hq") { id } }' \
  --jq '.data.repository.id'

# Discussion category IDs
gh api graphql -f query='
  { repository(owner:"mitodl", name:"hq") {
      discussionCategories(first:20) { nodes { id name } }
  } }' \
  --jq '.data.repository.discussionCategories.nodes[] | select(.name=="RFC") | .id'
```

---

## Step 1 — Confirm the RFC is ready to post

An RFC should only be posted after the design space has been explored. Before
writing the document, check:

- The problem is clearly articulated
- At least two options were considered (even if one was quickly ruled out)
- A decision or preferred direction exists (draft is fine; "TBD" is not)
- The key open questions are identified, even if unanswered

If planning is still in progress, finish that first.

---

## Step 2 — Write the RFC document

Use the standard RFC format below. Every section is required; delete only
**Open Questions** if there are genuinely none.

```markdown
# RFC: <title>

## Status
<Draft | Accepted | Superseded by #N | Withdrawn>

## Problem
What problem are we solving and why does it matter now?
One to three paragraphs. Include why this is worth addressing at this moment,
not just what it is.

## Options Considered

### Option N: <name>
One paragraph describing the approach.

**Tradeoffs:**
- ✅ pro
- ✅ pro
- ❌ con / risk

(Repeat for each option. Minimum two options.)

## Decision
Which option we're going with and why. Reference the tradeoffs above —
don't just restate them, explain the weighting. If the decision is a
stepping-stone to a fuller option, say so explicitly.

## Implementation Plan
Ordered, numbered steps. Each step should be concrete enough that a
different engineer could pick it up. Avoid vague steps like "build the thing".

1. **Step title** — one sentence description of what gets built/done and why.

## Consequences

**What we gain:**
- bullet

**What we give up / risks:**
- bullet

**Downstream changes:**
- bullet (who/what is affected by this decision)

## Open Questions
- **Question label:** One sentence framing the question. Note if it is
  blocking or non-blocking for the initial implementation.
```

---

## Step 3 — Write the RFC body to a file

Write the finished RFC markdown to a temporary file. Do not attempt to inline
it into a shell command — newlines and quotes will break the invocation.

```bash
cat > /tmp/rfc-body.md << 'RFCEOF'
<RFC content here>
RFCEOF
```

---

## Step 4 — Post the discussion

Use the script that ships with this skill. It handles GraphQL encoding,
the `RFC:` title prefix, and all error reporting — do not reconstruct the
API call manually.

```bash
SCRIPT="skills/process/create-ol-rfc-discussion/scripts/post-rfc-discussion.sh"

bash "$SCRIPT" -t "<RFC title>" -f /tmp/rfc-body.md
```

Alternative invocations:

```bash
# Pipe body via stdin
cat /tmp/rfc-body.md | bash "$SCRIPT" -t "<RFC title>"

# Pass body as a string (for short RFCs)
bash "$SCRIPT" -t "<RFC title>" -b "$(cat /tmp/rfc-body.md)"
```

The script outputs the discussion URL on success. Confirm it and share
with the user.

### Script reference

`scripts/post-rfc-discussion.sh`

| Flag | Required | Description |
|------|----------|-------------|
| `-t TITLE` | ✅ | Discussion title. `RFC: ` prefix is added automatically if absent. |
| `-f FILE` | one of `-f`/`-b`/stdin | Read body from a file path. |
| `-b BODY` | one of `-f`/`-b`/stdin | Body text passed directly. |
| _(stdin)_ | one of `-f`/`-b`/stdin | Body read from stdin if neither flag is given. |

---

## RFC Status values

| Status | Meaning |
|--------|---------|
| `Draft` | Posted for team input; decision not yet final |
| `Accepted` | Decision is final; implementation may be in progress |
| `Superseded by #N` | Replaced by a later RFC (link the discussion number) |
| `Withdrawn` | No longer being pursued; brief reason in the Problem section |

Update the status by editing the discussion body after the team has reached
consensus. Do not delete Draft RFCs — keep them as a record.

---

## Tips

- **Title format:** Always prefix the discussion title with `RFC: ` so it is
  scannable in the discussion list. The posting script enforces this
  automatically.
- **Options minimum:** Include at least two options even if one was an obvious
  non-starter. Documenting the rejected path prevents revisiting the same
  ground in future discussions.
- **Decision ≠ restatement:** The Decision section should explain the
  *weighting* of tradeoffs, not just repeat the winning option's pros. What
  made the cons acceptable?
- **Implementation steps should be handoff-ready:** Each step should name the
  artifact being produced (file, service, config, skill, etc.) and be concrete
  enough that a different engineer could execute it.
- **Consequences ≠ tradeoffs:** Tradeoffs live under each option. Consequences
  documents what changes *as a result of the decision* — downstream effects,
  things the team gains and gives up going forward.
- **Open questions are not todos:** An open question is something that needs a
  decision before or during implementation. Unresolved implementation details
  belong in the implementation plan, not here.
- **Body size:** GitHub Discussions have a 65,536-character body limit. For
  very long RFCs, summarise verbosely-documented options and link to supporting
  documents rather than embedding them.

## Self-contained RFCs

RFC discussions must be **self-contained**. Do not reference local files or
paths that are inaccessible to readers. Instead:

- **Reference code** via GitHub URLs with line numbers
- **Reference prior RFCs** by their discussion URL
- **Reference external docs** via public URLs
- **Include essential snippets inline** rather than describing them
- **Attach diagrams** by uploading images to the discussion after posting
