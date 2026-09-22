# Shared analysis brief (template)

Fill in the `{{...}}` placeholders and save as `<workdir>/BRIEF.md`. Every
analysis pass reads it first. Keep what's here; add subject-specific context
(role history, stack changes) under "Subject".

---

# Brief for style-analysis passes

## Subject

- Mode: {{person | team | repo}}
- Name: {{name}}
- Identities: {{logins and git author patterns, or repo list}}
- Known history: {{role/stack timeline visible in activity/yearly.md and repos.tsv,
  e.g. "2016-2019 Salt config management; 2020 moved to Pulumi; 2023 became lead"}}

## Goal

Longitudinal analysis of {{coding and communication style | team conventions}},
producing evidence that will be turned into CLAUDE.md / AGENTS.md instructions and
agent skills. Findings must be specific enough that an agent can follow them, and
backed well enough that a skeptical reader can check them.

## Baseline rule (AI contamination)

- Data before {{ai_cutoff}} is the **baseline**. Rules come from the baseline.
- Data from {{ai_cutoff}} on (era {{comparison era}}) is **comparison only**. For each
  trait, say whether it persists or shifts. Label anything that appears only in the
  comparison era "possibly AI-influenced" and don't base rules on it.
- Items tagged `[AI]` in corpora are excluded from baseline evidence even if dated
  earlier.
- A comparison-era change that has a dated, deliberate cause (a tool migration
  commit, an RFC) is a real change, not drift. Say which it is.

## Team-mode rules (skip for person/repo mode)

- The unit of analysis is the **convention**, not the person. Report agreement as
  "N of M members" or "% of authors", never rank or grade individuals.
- Separate **enforced** conventions (linter/formatter config, templates, CI checks,
  review comments that request the change) from **emergent** ones (most members do
  it, nothing enforces it). Enforced conventions get higher confidence.
- Note where one prolific member dominates a metric. Recompute it without them
  before calling it a team norm.
- Don't quote a member's words in a way that singles them out for criticism.
  Quote exemplars of the convention.

## Data

All paths are relative to this directory.

- `corpus/INDEX.md`: file list with entry counts and sizes. Start here.
- `corpus/commit_msgs_<ERA>.txt`: deduplicated non-merge human commits. Header:
  date, repo, sha, @author, `[AI]` flag, `[+ins/-del files=N]`.
- `corpus/prs_<ERA>.txt`: authored PRs. Header includes size, commit count, and
  `own_words_chars` (body length with template headers, HTML comments and
  checkboxes stripped). Use own words, not raw length, for any length claim.
- `corpus/reviews_<ERA>.txt`: reviews with state, body, inline comments with the
  last lines of the diff hunk they were attached to.
- `corpus/conversation_<ERA>.txt`: issue/PR conversation comments, opened issues,
  discussion posts and comments.
- `corpus/diffs_<ERA>.txt`: stratified code-diff sample, sections headed
  `[language] repo date`.
- `stats/commit_stats.md`: per-year/era/member commit metrics. Verify and extend;
  don't recompute blindly.
- `stats/ai_markers.md`: monthly AI-marker counts.
- `data/*.jsonl`: raw records if you need to count something the corpora don't show.
- `repos/<owner>__<name>`: git checkouts. **Read-only.** Some are symlinks to the
  user's real working copies: never run checkout, fetch, reset, gc, or anything
  that writes. Use `git log`, `git show <sha>:<path>`, `git grep <rev>`, with
  `--until={{until}}`. Blobless (partial) clones are slow for anything but log.

Eras: {{list eras with date ranges}}.

## Method

- Corpora can be large (hundreds of KB). Don't read a file whole. Read slices spread
  across the date range, and use grep or a short script for counts.
- Quantify: counts, percentages, rates per 1k words, per era. A trait without a
  number or a sample size is an impression.
- Check that a pattern isn't produced by a template, bot, email-reply footer, or
  tool (formatter, GitHub squash suffix, autofilled commit bodies) before attributing
  it to the subject. See `references/pitfalls.md` in the skill.
- Distinguish repo-wide conventions from the subject's own lines where it matters
  (use `git log --author` / blame on specific files).

## Output contract

Write ONE markdown file to `findings/<dimension>.md` with these sections:

1. **Stable traits**: hold across baseline eras. Each has 2-4 verbatim quotes or
   snippets with date + repo (+ sha or URL), plus a count or percentage when
   measurable.
2. **Evolution**: what changed, approximately when, and the trigger if the
   evidence shows one (a config commit, a template, a role change).
3. **Comparison era**: persists / shifts, with "possibly AI-influenced" labels.
4. **Anti-patterns**: things conspicuously absent, with the sample size that makes
   the absence meaningful.
5. **Candidate instructions**: imperative rules an agent could follow, each tagged
   high/medium/low confidence and pointing at the evidence above. Include 2-3 real
   exemplars (quotes, not invented text).
6. **Observed but don't encode**: real habits that would make agent output worse
   if copied (sparse tests, direct pushes, typos), with a one-line reason.

Evidence-bound: no claim without a pointer; mark inferences as inferred. Aim for
1,500-3,500 words. Write nowhere except `findings/`. Delete any scratch files you
create outside it.

Final reply: a 5-line summary and the path written.
