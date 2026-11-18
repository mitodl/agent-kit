---
name: create-ol-rfc-discussion
description: >
  Write and post an RFC as a GitHub Discussion in the mitodl/hq repository
  under the RFC category: an executive summary of a proposed project, feature,
  or goal, followed by real questions on what is still undecided. Use this
  skill when asked to write or post an RFC, propose a project or design for
  team feedback, or ask the team for input on a direction before committing to
  it.
license: BSD-3-Clause
metadata:
  category: process
---

# Create an RFC Discussion (`/olrfc`)

When the user runs `/olrfc`, or asks to post/create an RFC, guide them through
writing a **short** RFC that leads with an executive summary and asks the team
about what is still open, and posting it to `mitodl/hq` as a GitHub Discussion
in the **RFC** category.

## What an RFC is for

An RFC tells the team about a project, feature, or goal we intend to pursue,
and asks for their opinion on the parts that are still open. It succeeds when
the people who will build on it, operate it, or be affected by it know it is
coming and have said what they think. It is a request for conversation, not a
verdict to ratify and not a design document.

So the document leads with an executive summary a busy reader can stop after,
then asks real questions about what hasn't been decided. Everything else in it
is there so a reader can answer those questions.

The implementation detail follows later, as a spec opened against the target
repositories. Anything a reader does not need in order to understand the
proposal and weigh in on it belongs in the spec, not here. PRs and tickets
will cite that spec, so give its sections headings that say what they decide,
not bare numbers or letters.

| Belongs in the RFC | Belongs in the spec |
|--------------------|---------------------|
| What we propose and why now | Schemas, interfaces, function signatures |
| What is still undecided, and our current leaning | File and module layout |
| The shape of the work, not its steps | Ordered task breakdown, estimates |
| Who it affects and what it costs them | Test plans, migration scripts |
| Alternatives a reader would reasonably ask about | Config keys, flag names, env vars |
| Links to prior art / related RFCs | Error handling, edge cases |

If you catch yourself writing code blocks, type definitions, or a numbered
build plan, you have crossed into spec territory. Cut it.

**If nothing is undecided, it isn't an RFC.** A settled plan with no question
for the team is an announcement: post it as one, or record it as an ADR in the
repo it affects. Don't invent questions to fill the section.

---

## Length budget — the ceiling is a hard requirement

**Target: 500–1,500 words. Ceiling: 2,000.** That is at most a 5–10 minute
read. RFCs that run 20–30 minutes do not get read, and unread RFCs get no
answers. The floor is a guide, not a quota: a small proposal with one open
question can be shorter, and padding it to reach a number defeats the point.

Rough per-section budget:

| Section | Budget |
|---------|--------|
| Summary | 100–200 words |
| Where we want your input | 150–400 words |
| Background | 150–300 words |
| The proposal | 200–400 words |
| Who this affects | 100–200 words |

Before posting, check it:

```bash
wc -w "$RFC_BODY"
```

If it is over 2,000, cut — do not rationalise. The usual offenders, in order:
step-by-step implementation plans, a survey of every alternative, background
the Summary already gave, and worked examples.

---

## Step 1 — Confirm the RFC is ready to post

Post an RFC when there is a direction worth reacting to and something real
left to decide. Check:

- The goal is clear: what we want to be true, and why now
- A proposed direction exists, even a rough one. "We don't know where to
  start" is a conversation to have first, not an RFC
- At least one question is genuinely open, and the team's answer could change
  what we do

If nothing is open, see [What an RFC is for](#what-an-rfc-is-for): it is an
announcement or an ADR.

---

## Step 2 — Get the content from the operator

An RFC is an argument its author has to stand behind in review. **Ask the user
for the substance of each section rather than composing it for them**, in a
single batched question:

| Section | What to ask for |
|---------|-----------------|
| Summary | What we're proposing, why now, and who it's for |
| Input | What is still undecided, their current leaning on each, and whose opinion they most want |
| Background | What's broken, blocked, or newly possible |
| Proposal | The shape of the work, what's already settled and why, and any alternative a reader will ask about |
| Who this affects | Teams, services, or repos that inherit work or change behaviour |

Exception: when the design work happened in this session, you already have the
material — draft from it, but show the draft and let the user correct the
framing before posting. Which questions they actually consider open, and their
leaning on each, is the part that matters, and it is not recoverable from the
code.

Use their phrasing where you have it. Your job is compression and structure,
not authorship — never expand a one-line answer into a paragraph to fill a
section. A thin section means the design work isn't done; say so rather than
padding it.

## Step 3 — Write the RFC document

Summary and "Where we want your input" are required. Drop Background when the
Summary already says enough about why, and drop "Who this affects" when the
answer is only the authoring team.

Note the line discipline in the template: every paragraph and every bullet is
a single long line. Keep it that way as you fill it in — see
[Line breaks](#line-breaks) below for why.

```markdown
# RFC: <title>

## Status
<Draft, input wanted by <date> | Accepted | Superseded by #N | Withdrawn>

## Summary
One short paragraph: what we propose to do, why now, and what changes for whom. A reader who stops here should know what is being proposed and that we want their opinion on it.

## Where we want your input
- <A question that is genuinely open, written so it makes sense on its own.> Our current leaning and why, in a sentence or two, or "no leaning yet". Say what would change the answer, and name the people or teams whose view matters most here.

(One bullet per open question. Ask only what we'd act on: if every answer leads to the same plan, it isn't open.)

## Background
What is broken, blocked, or newly possible, and what changed to make this worth doing now. Two short paragraphs at most.

## The proposal
The shape of the work at the altitude of "what changes where": the systems and repos touched, not the files. Say which parts are already settled and why, so readers spend their attention on the open ones. When a reader will reasonably ask "why not X", answer it here in a sentence or two of prose. Don't build a menu of options to make the choice look rigorous.

## Who this affects
Teams, services, or repos that inherit work or change behaviour, and what it costs them.
```

### Line breaks

Write each paragraph and each bullet as one long line and let GitHub wrap it.
GitHub's discussion renderer turns a single newline inside a paragraph into a
line break, so an 80-column wrap that looks tidy in the heredoc renders as a
column of ragged short lines. Newlines belong between blocks, between list
items, and inside code fences — nowhere else. This applies to the RFC body you
post, not to this file.

---

## Step 4 — Write the RFC body to a file

Write the finished RFC markdown to a temporary file. Do not attempt to inline
it into a shell command — newlines and quotes will break the invocation, and
backticks and `$` are expanded by the shell before the script sees them.

Name the file with `mktemp`, not a fixed path like `/tmp/rfc-body.md`. Several
agents can run this skill at once across different checkouts on one machine,
and a shared name means one of them broadcasts another's draft under its own
title.

```bash
RFC_BODY="$(mktemp -t rfc-body.XXXXXXXX)"
cat > "$RFC_BODY" << 'RFCEOF'
<RFC content here>
RFCEOF
```

Then run the word count from the length budget section above before posting.
Keep Steps 4 and 5 in one shell invocation so `$RFC_BODY` is still set, or
capture the path and reuse it literally.

---

## Step 5 — Post the discussion

Show the user the finished RFC and get explicit confirmation before posting —
this is a team-wide broadcast, not a draft they can quietly amend.

Use the script that ships with this skill. It handles GraphQL encoding,
the `RFC:` title prefix, and all error reporting — do not reconstruct the
API call manually.

```bash
SCRIPT="skills/process/create-ol-rfc-discussion/scripts/post-rfc-discussion.sh"

bash "$SCRIPT" -t "<RFC title>" -f "$RFC_BODY"
```

Piping via stdin works too:

```bash
bash "$SCRIPT" -t "<RFC title>" < "$RFC_BODY"
```

The script also accepts `-b BODY`. Don't reach for it. Typing the RFC text
into it directly reintroduces the expansion the file avoids, and the
`-b "$(cat …)"` spelling — which is expansion-safe, since a command
substitution's output is not re-expanded — just routes a 2,000-word body
through the argument list for no gain.

The script outputs the discussion URL on success. Confirm it and share
with the user.

### Script reference

`scripts/post-rfc-discussion.sh`

| Flag | Required | Description |
|------|----------|-------------|
| `-t TITLE` | ✅ | Discussion title. `RFC: ` prefix is added automatically if absent. |
| `-f FILE` | one of `-f`/`-b`/stdin | Read body from a file path. Use this. |
| `-b BODY` | one of `-f`/`-b`/stdin | Body text passed directly. Avoid — typed literally, the shell expands its backticks and `$` first. |
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
| `Draft` | Posted for team input; the open questions are still open |
| `Accepted` | The open questions are answered and we're proceeding |
| `Superseded by #N` | Replaced by a later RFC (link the discussion number) |
| `Withdrawn` | No longer being pursued; brief reason in the Summary |

When the discussion settles a question, edit the body to record the answer
under that question rather than deleting it, so a later reader sees what was
asked and what the team chose. Do not delete Draft RFCs — keep them as a
record.

---

## Writing tips

- **Write for a reader who will skim.** The Summary and the questions should
  carry the RFC on their own. If someone reads only those, they should know
  what we propose and what we're asking them.
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
- **Ask real questions.** A question is open when we don't know the answer
  and the team's answer would change what we do. "Does this sound good?" is not
  one. Neither is a choice already made, phrased as a question to look
  consultative.
- **Give your leaning.** "We lean toward X because Y; tell us if Z makes
  that wrong" gets a sharper answer than a neutral list of possibilities.
- **Ask the right people.** When a question needs a particular team's or
  person's knowledge, say so (`@handle` or the team name), so it doesn't wait
  on someone assuming it's for somebody else.
- **Alternatives are answers, not a section.** Mention a road not taken
  where a reader would ask about it, in a sentence or two. There is no required
  number of them.
- **The proposal ≠ an implementation plan.** Name the pieces of work and where
  they land. If a sentence could be a ticket title, it is at the right
  altitude; if it reads like a ticket body, cut it down.
- **Open questions are not todos.** Unresolved implementation detail belongs
  in the spec.
- **Don't label the questions.** Write each one so a reply can quote or name
  it ("on the batch size question"). `Q1`, `Option B` and `D3` mean nothing to
  someone reading only the comment thread.
- **Link instead of re-explaining, and say what the link holds.** Prior art,
  benchmark output, and design explorations belong behind a URL, with a
  clause saying what the reader will find there.

## Self-contained RFCs

RFC discussions must be **self-contained**. Do not reference local files or
paths that are inaccessible to readers. Instead:

- **Reference code** via GitHub URLs with line numbers
- **Reference prior RFCs** by their discussion URL
- **Reference external docs** via public URLs
- **Attach diagrams** by uploading images to the discussion after posting
- **Name what you point at.** A label a document gives its own parts (§4,
  D3, Phase 0, Cohort 4, open question 2) means nothing outside it. Say what
  the part is and link its heading: "the row-count guard in the
  [scoped-pull contract](https://github.com/mitodl/ol-data-platform/blob/main/docs/design/contentfile_scoped_pull_contract.md#4-the-scoping-problem--the-part-that-needs-new-code)",
  not "the §4 guard"

Self-containment means every reference must resolve for a reader who does not
have your checkout — it is not licence to paste the detail back in. If a
snippet feels necessary to carry the argument, that is the tripwire above
firing: the proposal is resting on something that belongs in the spec.
Describe the shape in a sentence and link to the code or the draft spec.
