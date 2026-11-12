# Ranking rubric

The six criteria the [`plan-technical-approach`](../SKILL.md) skill scores every
candidate approach against, each with what counts as evidence for it and a
worked example contrasting a real finding with a look-alike that is not one.

The distinction in every pair is the same: a real score cites something that
was measured or read while grounding; a look-alike is a preference, a vibe, or a
hypothetical that nothing in the codebase supports.

Score each criterion **better / neutral / worse**, relative to doing nothing.
Not a number — a 1-to-5 invites averaging, and averaging hides the one
criterion that should decide the call.

## 1. Tech-debt delta

Does this leave less debt than it found, or more? Evidence is a count: call
sites consolidated or duplicated, a workaround removed or added, a TODO closed
or opened.

**Real:** "Removes the second copy of the retry logic. `code_callers` shows
both copies are reachable from the same scheduler path, and they have already
drifted — one honours `max_attempts`, the other doesn't."

**Not real:** "Cleaner and more maintainable." Nothing was counted. Every
approach claims this about itself.

## 2. Complexity delta

Does the system get easier or harder to hold in your head? Evidence is a
structural change: a branch removed, an indirection added, a state machine that
gains or loses a state.

**Real:** "Adds a third caching layer between the serializer and the queryset.
A reader debugging a stale response now has three places to check instead of
one, and nothing tells them which was consulted."

**Not real:** "Adds about 80 lines." Line count is not complexity. A long
straight-line function can be simpler than a short clever one.

## 3. Architectural fitness

Does this go with the grain of the codebase, or across it? Evidence is a
comparable: another feature in the same repo that solves the same shape of
problem, and whether this matches it.

**Real:** "Every other cross-service read here goes through the client wrapper
in `clients/`. This one opens its own session, so it skips the retry policy and
the auth refresh that wrapper exists to apply."

**Not real:** "This is the more idiomatic design." Idiomatic according to what?
If no comparable in this repo was named, this is taste.

## 4. Blast radius and risk

What breaks if this is wrong, and how far does the damage reach? Evidence is a
reach measurement — `code_impact`, caller counts, or whether the change is
behind a flag.

**Real:** "Changes the signature of `Service.run`, which `code_callers` puts at
14 call sites across three repos. Two are in `ol-infrastructure` and deploy on
a different cadence, so they break between deploys, not at merge."

**Not real:** "Low risk, it's a small change." Small diffs in shared code have
the largest radius; size and reach are different measurements.

## 5. Effort

What does it cost to build? Evidence is the work itself: files to touch,
migrations needed, tests to write, coordination with another team.

**Real:** "Needs a backfill over ~4M rows and a two-deploy sequence to stay
compatible, so it cannot land in one PR."

**Not real:** "A couple of hours." A time estimate with no decomposition behind
it is a guess. Name the work, not the duration.

**Note how it chunks.** Approaches differ in whether they can be delivered in
reviewable pieces, and that is decision-relevant rather than a detail to settle
afterwards: an approach that lands in three 300-line PRs along layer lines is
cheaper to review and safer to revert than one that is a single unsplittable
900-line change, even at identical total effort. Score the unsplittable one
worse here and say why. See
[`chunking-the-work.md`](chunking-the-work.md) for the axes and the size
budget.

## 6. Reversibility

If this turns out wrong in a month, how expensive is backing it out? Evidence
is the reversal path: does it survive a revert, or has data or an external
contract moved?

**Real:** "Reverting restores behavior, but rows written under the new schema
keep the new shape. The revert needs a down-migration that does not exist yet."

**Not real:** "Easy to undo later." If the reversal path was not described, it
was not checked — and this is the criterion most often assumed.

---

## The tradeoff ledger

Every accepted tradeoff gets a row. A hack with no ledger row is not an
approved hack — the point of the ledger is that in six months someone can tell
a deliberate compromise from an oversight.

| Field | What goes in it |
|-------|-----------------|
| **Giving up** | The concrete thing being sacrificed, not "some cleanliness" |
| **To get** | The concrete thing bought, with the reason it matters now |
| **Revisit when** | An observable signal — a date, a metric threshold, a dependency landing. Not "when we have time" |
| **Decided by** | The person who made the call. The agent is never the decider |
| **Follow-up** | For an accepted workaround, the URL of the issue tracking the real fix. Required — see [The follow-up issue](#the-follow-up-issue). Empty for a tradeoff that is a permanent, deliberate choice rather than deferred work |

Two entries the ledger must also carry, and which survive a reader who knows
whose idea was whose:

- **Against the recommendation:** the strongest argument that it is the wrong
  call. If there is genuinely none, say that — but it is rarer than it looks.
- **For each rejection:** the condition under which that option would have won.
  This is what makes a rejection reviewable instead of a verdict.

## Accepting a short-term hack

A workaround is an acceptable recommendation. It is not acceptable *undocumented*.
Before recommending one, all five must be answerable:

1. **Named cost** — what specifically gets worse, in the terms of criteria 1–6.
2. **Named benefit** — what it buys, and why that matters on this timeline
   rather than in general.
3. **Named revisit trigger** — the observable signal that says the debt is now
   due.
4. **Named decider** — who accepted it.
5. **A follow-up issue** describing the real fix — drafted before approval,
   opened when the plan is recorded. See below.

If any is unanswerable, it is not a hack with known tradeoffs; it is an unknown
tradeoff, and the honest move is to say so rather than to recommend it.

## The follow-up issue

**An accepted workaround that leaves no tracked artifact is just a bug someone
chose.** The ledger row lives on an issue comment that nobody will find from
the code; the follow-up issue is what makes the debt visible in the backlog
where work actually gets prioritized.

So: when a workaround is the recommendation, open an issue describing the real
fix. Not optional, and not deferred to "when we get around to it" — a plan
whose ledger row has no issue URL is not a finished plan.

**When.** Draft it while scoring, show it as part of the approval so the
engineer approves the workaround and its follow-up together rather than being
asked twice, and open it when the plan is recorded — before the plan comment,
so the ledger row can carry a URL instead of a promise.

**Where.** The same repo as the originating issue, unless the debt is
unambiguously in a different product repo — that is where the team already
looks. Use the Technical Issue template from
`create-ol-github-issue`,
which is the engineering-work shape; this is not a bug report. Apply a
debt-tracking label only if the repo already has one. Do not invent a label.

**Content.** It has to stand on its own: whoever picks it up has none of this
session's context, and the plan comment may be buried under fifty comments by
then.

```markdown
### Description/Context

<What the workaround does, in one or two sentences, and where it lives.>

Shipped deliberately as a short-term measure in <originating issue URL>. The tradeoff was accepted by @<decider>: <what we gave up> in exchange for <what it bought>.

**Revisit when:** <the observable trigger from the ledger>

**Why the real fix was deferred:** <the actual reason — a deadline, a blocking dependency, an unknown that needed production data. Not "time constraints".>

### Plan/Design

<What the real fix is, concretely enough to be estimated. If it is genuinely unknown, say what would have to be learned first — an unknown fix is still worth tracking, but say it is unknown rather than leaving the section empty.>
```

**Link it in three directions**, or it is findable from only one:

- The follow-up issue names the originating issue.
- The plan comment's ledger row carries the follow-up issue URL.
- The code carries a comment at the workaround pointing at the follow-up issue,
  so someone reading the code finds the ticket without knowing it exists. This
  is the link that matters most and the one most often skipped;
  [`implement-plan`](../../implement-plan/SKILL.md) lands it in the same pull
  request as the workaround.

A fourth lands later: when the workaround's pull request merges, add its URL to
the follow-up issue, since that is the diff whoever fixes it has to undo.

**Not to be confused with a deferred chunk.** Work deliberately scheduled for a
later pull request belongs in the Delivery table in
[`chunking-the-work.md`](chunking-the-work.md) — it is planned work with a
known slot. A follow-up issue is for debt with no slot. Work that is both
(shipping the workaround now, with the real fix already scheduled) gets both:
the chunk row and the issue.

## Scoring a sole candidate

The rubric applies unchanged when there is only one approach. Nothing is being
ranked, but scoring still surfaces the tradeoffs, the blast radius and the
reversal path — which is most of the value, and all of it when the engineer
has already decided *what* to do and needs to know what it costs.

Say explicitly that there was one candidate. A ledger that looks like the
output of a comparison, when no comparison happened, misleads exactly the
reader it was written for.
