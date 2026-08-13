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
writing a **short, high-altitude** RFC and posting it to `mitodl/hq` as a
GitHub Discussion in the **RFC** category.

## What an RFC is for

An RFC exists to get a team of busy people to agree on **what problem we're
solving, roughly how we intend to solve it, and what that commits us to**.
It is a decision document, not a design document.

The implementation detail follows later, as a spec opened against the target
repositories. Anything a reader does not need in order to say "yes, go do
that" belongs in the spec — not here.

| Belongs in the RFC | Belongs in the spec |
|--------------------|---------------------|
| The problem and why now | Schemas, interfaces, function signatures |
| The 2–3 real options and why they lose/win | File and module layout |
| The chosen direction, in shape not steps | Ordered task breakdown, estimates |
| What the decision commits us to, and what it costs | Test plans, migration scripts |
| Questions that could change the decision | Config keys, flag names, env vars |
| Links to prior art / related RFCs | Error handling, edge cases |

If you catch yourself writing code blocks, type definitions, or a numbered
build plan, you have crossed into spec territory. Cut it.

---

## Length budget — this is a hard requirement

**Target: 800–1,500 words. Ceiling: 2,000.** That is a 5–10 minute read.
RFCs that run 20–30 minutes do not get read, and unread RFCs do not produce
decisions.

Rough per-section budget:

| Section | Budget |
|---------|--------|
| Problem | 150–300 words |
| Options Considered | 300–500 words total across all options |
| Decision | 150–250 words |
| Approach | 150–300 words |
| Consequences | 100–200 words |
| Open Questions | 75–150 words |

Before posting, check it:

```bash
wc -w /tmp/rfc-body.md
```

If it is over 2,000, cut — do not rationalise. The usual offenders, in order:
step-by-step implementation plans, exhaustive option enumeration, restating
the problem inside the Decision section, and worked examples.

---

## Step 1 — Confirm the RFC is ready to post

An RFC should only be posted after the design space has been explored. Check:

- The problem is clearly articulated
- At least two options were considered (even if one was quickly ruled out)
- A decision or preferred direction exists (draft is fine; "TBD" is not)
- The key open questions are identified, even if unanswered

If planning is still in progress, finish that first.

---

## Step 2 — Get the content from the operator

An RFC is an argument its author has to stand behind in review. **Ask the user
for the substance of each section rather than composing it for them**, in a
single batched question:

| Section | What to ask for |
|---------|-----------------|
| Problem | What's broken/blocked, and why now |
| Options | The 2–3 approaches considered, and the tradeoff on each |
| Decision | Which one, and what made its costs acceptable |
| Approach | The major pieces of work, one line each |
| Consequences | What the team gains, gives up, and who inherits work |
| Open Questions | What could still change the decision |

Exception: when the design work happened in this session, you already have the
material — draft from it, but show the draft and let the user correct the
framing before posting. Their weighting of the tradeoffs is the part that
matters, and it is not recoverable from the code.

Use their phrasing where you have it. Your job is compression and structure,
not authorship — never expand a one-line answer into a paragraph to fill a
section. A thin section means the design work isn't done; say so rather than
padding it.

## Step 3 — Write the RFC document

Every section is required; delete only **Open Questions** if there are
genuinely none.

```markdown
# RFC: <title>

## Status
<Draft | Accepted | Superseded by #N | Withdrawn>

## Problem
What problem are we solving, and why does it matter now? Two or three short
paragraphs. Lead with the consequence of not acting. Say what changed to make
this worth doing at this moment.

## Options Considered

### Option N: <name>
Two to four sentences describing the approach in terms of its *shape* — what
moves, what stays, who owns it. Not how it is built.

**Tradeoff:** one or two sentences on what this option buys and what it costs.
Use a short bullet pair only if the tradeoff genuinely has multiple axes.

(Repeat for each option. Two or three options; more than three means the space
was not narrowed before writing.)

## Decision
Which option we're going with and *why that tradeoff is the right one to
accept*. Do not restate the option's pros — explain the weighting. If this is
a stepping-stone toward a fuller option, say so.

## Approach
The shape of the work in three to six bullets — the major pieces, roughly in
order, at the altitude of "what changes where". Name the systems and repos
touched, not the files. The detailed breakdown lands in the spec.

- **<Piece of work>** — one sentence on what changes and where.

## Consequences

**What we gain:** two or three bullets.

**What we give up / risks:** two or three bullets.

**Who is affected:** teams, services, or repos that inherit work or change
behaviour as a result.

## Open Questions
- **Question label:** one sentence. Mark blocking vs. non-blocking.
  Only list questions whose answers could change the decision or the approach —
  unresolved implementation detail goes in the spec.
```

---

## Step 4 — Write the RFC body to a file

Write the finished RFC markdown to a temporary file. Do not attempt to inline
it into a shell command — newlines and quotes will break the invocation.

```bash
cat > /tmp/rfc-body.md << 'RFCEOF'
<RFC content here>
RFCEOF
```

Then run the word count from the length budget section above before posting.

---

## Step 5 — Post the discussion

Show the user the finished RFC and get explicit confirmation before posting —
this is a team-wide broadcast, not a draft they can quietly amend.

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

## Writing tips

- **Write for a reader who will skim.** Section headings and first sentences
  should carry the argument on their own. If someone reads only the first line
  of each paragraph, they should still get the decision.
- **Prefer the general over the particular.** "Sessions are held in process
  memory, so we cannot scale horizontally" beats three paragraphs on the
  session dict's structure.
- **One idea per paragraph, three sentences per paragraph.** Dense
  multi-clause prose reads as long even when the word count is fine.
- **Plain words, short sentences.** No preamble, no scene-setting, no closing
  summary, no filler adjectives ("comprehensive", "robust", "significant"), no
  emoji. Bullets where the content is a list.
- **Title format:** always prefix the title with `RFC: `. The posting script
  enforces this automatically.
- **Two options minimum, three maximum.** Document the rejected path so the
  same ground is not revisited — but a survey of six approaches means the
  design work was not finished before writing.
- **Decision ≠ restatement.** Explain the *weighting* of the tradeoffs, not
  the winning option's pros. What made its costs acceptable?
- **Approach ≠ implementation plan.** Name the pieces of work and where they
  land. If a bullet could be a ticket title, it is at the right altitude; if it
  reads like a ticket body, cut it down.
- **Consequences ≠ tradeoffs.** Tradeoffs live under each option. Consequences
  are what changes for the team *as a result of the decision*.
- **Open questions are not todos.** They are things that could change the
  decision. Everything else is spec material.
- **Link instead of embedding.** Prior art, benchmark output, long option
  writeups, and design explorations belong behind a URL.

## Self-contained RFCs

RFC discussions must be **self-contained**. Do not reference local files or
paths that are inaccessible to readers. Instead:

- **Reference code** via GitHub URLs with line numbers
- **Reference prior RFCs** by their discussion URL
- **Reference external docs** via public URLs
- **Attach diagrams** by uploading images to the discussion after posting

Self-containment means every reference must resolve for a reader who does not
have your checkout — it is not licence to paste the detail back in. If a
snippet feels necessary to carry the argument, that is the tripwire above
firing: the decision is resting on something that belongs in the spec.
Describe the shape in a sentence and link to the code or the draft spec.
