# Agent Communication Style — PRs, Issues, RFCs, Reviews

Drop-in fragment for a repo's `AGENTS.md` or a user's `CLAUDE.md`. Paste the
section below as-is, or merge it into an existing "Writing Style" /
"Communication Style" section.

Scope: text an agent writes that **other people read and are expected to act
on or discuss** — PR descriptions, issue bodies, RFC documents, review
replies, discussion comments. It is not about interactive chat replies to the
operator, which usually already have their own terser convention (see the
skills in this repo's `skills/process/` category for format-level rules
specific to PRs, issues, and RFCs — required sections, length budgets, when to
link vs. inline). This fragment is about *voice*: the thing that makes agent
output read as generated even when the structure is correct.

```markdown
## Communication Style — PRs, Issues, RFCs, Reviews

Write like the engineer who did the work, explaining it to a teammate — not
like a report generated about the work.

- **Read the room first.** Skim a few recent PRs/issues/RFCs in this repo
  before writing. Match the team's actual register — how formal, how terse,
  whether headers get used at all — instead of applying a fixed template
  everywhere.
- **Skip the AI tells.** No throat-clearing ("I'll go ahead and...", "Let's
  take a look at..."), no corporate transitions ("Furthermore,"
  "Additionally,"), no inflated adjectives (robust, seamless, powerful,
  comprehensive, significant), no em dash as a crutch, no closing summary that
  restates what the body just said.
- **Specifics beat enthusiasm.** "Fixes the race between the retry loop and
  the cancel handler" reads as human. "This is a great improvement to
  reliability!" reads as generated. State what changed and why; skip the
  praise.
- **Don't hedge what you verified, and don't assert what you didn't.** A flat,
  confident sentence about something you checked reads as human. The same
  sentence about something you didn't check reads as a claim waiting to be
  caught — say what's unverified instead of padding it in "should" and
  "likely."
- **Say it once.** A PR description that restates the title, or a testing
  section that re-narrates the description, reads as filler even when each
  sentence is individually fine.
- **Structure serves the reader, not a checklist.** Bullets for a list of
  changes; a couple of plain sentences for a couple of plain facts. Don't
  stretch two sentences into five bullet points to look thorough — that's the
  "robotic" tell people are reacting to.
- **Brevity is not a shortcut being skipped.** A two-sentence PR body that
  fully orients the reviewer is complete. Don't pad it to look more finished.
- **No performative caveats.** Skip disclaimers nobody asked for ("as an
  AI...", "please double-check this before merging"). If something is
  genuinely uncertain, say what's uncertain and why, once, and move on.
- **Write for someone who has to act on it.** A reviewer needs to know what to
  check; a teammate triaging an issue needs to know what's actually broken.
  Optimize for that, not for looking exhaustive.

Where this conflicts with an established house style in a given repo, the
house style wins.
```

## Why this exists

Structural rules (required sections, bullets vs. paragraphs, no filler
adjectives) are necessary but not sufficient — a PR body can follow every
formatting rule and still read as generated. The tells that make people
disengage are about voice: throat-clearing openers, corporate transitions,
inflated adjectives, restating context the reader already has, and
enthusiasm standing in for specifics. Centralizing that guidance here means
one place to update instead of re-deriving it per skill or per repo.

## Related

- `skills/process/create-ol-pull-request/SKILL.md`, `create-ol-github-issue`,
  `create-ol-rfc-discussion` — format-level writing-style rules scoped to
  each artifact type (length budgets, required sections, filler-adjective
  list). This fragment is upstream of those: it's the voice; they're the
  shape.
- `skills/process/address-pr-feedback/SKILL.md` — reply style for review
  threads ("one or two sentences... never resolve a thread by silently
  ignoring it, and never reply with a content-free 'noted'").
