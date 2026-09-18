# Report template

The synthesis turns `findings/*.md` into one document meant as source material for
CLAUDE.md / AGENTS.md instructions and skills. Keep the evidence in the findings
files and cite them (`[commits §2]`); the report carries conclusions, counts, and a
few exemplars.

Write it in the voice the analysis found, where that's appropriate for a technical
document: if the subject never uses bold-label bullets or closing summaries, the
report shouldn't either.

---

```markdown
# <Name>: <longitudinal style profile | team conventions | repository conventions>

Source material for CLAUDE.md / AGENTS.md instructions and agent skills.
Generated <date> from <sources>, <since>-<until>.

## How to use this document
(One line per part, saying what it's for.)

## Part 1. Method and caveats
- Data table: repos, commits (deduped), PRs, issues, comments, reviews, discussions, code
  sampled.
- Eras and why the boundaries sit where they do.
- The AI boundary: first markers, chosen cutoff, how the comparison era is used.
- Limits: invisible private activity, shared-repo attribution, thin samples, regex
  approximations.
- What was spot-checked during synthesis.

## Part 2. Era map
Table: era, role/stack context, communication shape. Then 2-3 sentences on what
triggered changes (tools, templates, role changes vs unexplained drift).

## Part 3. The stable core
Numbered traits that hold across baseline eras and across media (commits, PRs,
reviews, prose, code). Each: one-line rule, a count, one short exemplar, citation.
These are the highest-confidence rules; put them first because they generalize.

## Part 4. Domain profiles
One subsection per findings file: evolution table, stable habits with numbers,
candidate rules tagged high/medium/low, one or two exemplars.
(Team mode: add an "enforced vs emergent" column to each rule.)

## Part 5. Comparison-era drift to avoid copying
Table: artifact, marker that first appears after the cutoff, baseline counterpart.
Then a sentence on what did *not* change.

## Part 6. Observed but not worth encoding
Table: habit, evidence, recommendation (ignore / invert / encode a narrower version).

## Part 7. Audit of existing instructions
Only if the subject has CLAUDE.md / AGENTS.md / style guides / skills. Table:
current instruction, verdict (corroborated / stricter than baseline / not in
baseline / contradicted), evidence. Then a list of well-evidenced rules missing from
the current instructions.

## Part 8. Paste-ready material
### 8.1 Instruction block
A fenced markdown block ready to paste into CLAUDE.md / AGENTS.md: only high and
solid medium confidence rules, written as short imperatives, grouped by Voice /
Commits / PRs / Reviews / Code / Infra.
### 8.2 Skill changes
Table: existing or new skill, change, source section.

## Appendix: data locations
Where findings, corpora, stats and raw data live, and how to rebuild.
```

## Synthesis rules

- **Promote a trait to Part 3 only if it shows up in at least two media or two
  baseline eras.** Single-dimension traits stay in Part 4.
- **Every rule carries a confidence and a pointer.** Rules from the comparison era
  alone don't go in Part 8.
- **Spot-check before publishing.** Verify at least one headline number or quote per
  findings file against the source (the commit, the template file, a grep count).
  Say in Part 1 which ones you checked. Correct or drop anything that doesn't hold.
- **Resolve conflicts between passes explicitly.** If the commits pass and the PR
  pass disagree on a date or a count, check the data and state the right one.
- **Separate description from prescription.** Part 4 describes; Part 8 prescribes. A
  habit that makes output worse goes to Part 6, not Part 8.
- **Team mode:** no per-person tables or rankings. Report conventions with agreement
  levels ("7 of 9 members", "enforced by ruff since 2023-02").
