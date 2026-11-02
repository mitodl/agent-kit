# Analyst brief

The brief handed to the context that scores the candidates in Step 3 of
[`plan-technical-approach`](../SKILL.md). Paste it with the placeholders filled in.

It is kept here, out of `SKILL.md`, for two reasons: it is long and read once,
and it must be handed over *verbatim* — paraphrasing it is how the attribution
leaks back in.

## What must not be in the brief

The brief is the entire context the analyst gets. Before sending, check that it
carries none of:

- Which candidate the engineer proposed, or any first-person phrasing that
  gives it away. Candidates are normalized into the Step 2 template and
  relabeled A/B/C alphabetically on the mechanism line, precisely so this check
  is mechanical rather than a matter of care.
- The conversation, the issue thread, or a summary of either. The analyst
  scores mechanisms against evidence, not a discussion.
- Any hint of which option you expect to win — including ordering that is not
  the stated alphabetical rule, or a candidate written in more detail than its
  siblings. Unequal detail is the most common tell.
- A label like "the quick fix" or "the proper solution". Those are conclusions
  wearing the costume of descriptions.

Candidates carrying a genuine "short-term workaround" property keep it: it is a
real attribute of the mechanism, it goes in the same field for every candidate
that has it, and the rubric handles it explicitly.

## The brief

```text
Score each approach below against the rubric. Do not implement anything, and
do not edit any file.

GOAL
<one or two sentences, from the issue — not from the discussion of it>

REPOSITORY
<path>, branch <branch>

WHAT THE CODE LOOKS LIKE TODAY
<the measured baseline from scoping: where the behavior lives, what calls it,
caller counts, touch-points. Facts only — no direction.>

HARD CONSTRAINTS
<constraints the engineer stated, each of which every candidate must satisfy. A
candidate that violates one is scored as failing, not penalized.>

CANDIDATES

A.
Mechanism:        <...>
Touches:          <...>
Explicitly not:   <...>
Failure mode:     <...>
Reversal path:    <...>

B.
<same five fields>

RUBRIC
Score every candidate on all six criteria as better / neutral / worse relative
to doing nothing: tech-debt delta, complexity delta, architectural fitness,
blast radius and risk, effort, reversibility. The definitions and the
real-vs-look-alike examples are in references/ranking-rubric.md — apply that
file's evidence bar: a score cites something countable from the baseline above
or from the code, never a preference.

BUDGET
<N> tool calls. Read the code to verify a claim about it; stop looking for new
considerations once ~80% is spent and finish scoring what you have. Do not
query live systems. If something cannot be settled within the budget, say so
and score it as unknown rather than guessing.

RETURN
1. A table: candidate x criterion, with a one-line justification per cell that
   names its evidence.
2. A recommendation, and the single criterion that decided it.
3. The strongest argument AGAINST your recommendation.
4. For each candidate you did not recommend, the condition under which it would
   have won.
5. A tradeoff ledger row for anything the recommendation gives up: giving up /
   to get / revisit when. Leave "decided by" blank — that is the engineer's to
   fill, not yours.
6. Anything you could not verify within the budget.

If two candidates score identically, say so rather than breaking the tie on
instinct. A tie is information: it usually means they are the same mechanism,
or the deciding criterion is not in the rubric.
```

## With a single candidate

Send the same brief with one candidate and a changed RETURN: drop items 2 and
4, keep the scored table, the argument against, the ledger rows and the
unverified list. Add one line to the brief: `There is one candidate. Score it;
there is nothing to rank it against.` Without that line an analyst will invent
a comparison to fill the shape the brief implies.
