# Status lifecycle and revisions

How an ADR written by [`write-adr`](../SKILL.md) becomes accepted, and what
happens when the decision it records changes.

## Status values

`Proposed | Accepted | Rejected | Deprecated | Superseded by ADR-XXXX`

- **Proposed** — decided by the author, not yet agreed.
- **Accepted** — agreed and in force.
- **Rejected** — considered and not chosen. The file stays, with the reasoning.
- **Deprecated** — no longer recommended, still in use.
- **Superseded** — replaced. Links forward to the replacement.

## An agent writes Proposed, never Accepted

ol-infrastructure's guide is explicit that only humans accept, and an agent
leaves `Proposed` with "Created by AI agent, pending human approval".

That rule has already lost twice: ADRs 0003 and 0004 read `**Status:**
Accepted` alongside `**Deciders:** AI Agent (GitHub Copilot CLI) + Human
validation pending` — an agent wrote Accepted and wrote "pending" in the next
field. Do not do that. An ADR is either agreed or it is not.

## Acceptance is an event, not a chore

The rule's cost is also visible: four of ol-infrastructure's 13 ADRs are still
`Proposed`, the oldest since 2025-10-29, while the decisions they describe are
long since in production. The README's suggested monthly ADR review has, on
this evidence, never happened.

So acceptance is tied to something that actually occurs:

**Merging the implementation pull request with a human approving review is the
acceptance event.**

[`implement-plan`](../../implement-plan/SKILL.md) performs it at chunk
close-out:

```bash
# Each reviewer's LATEST review only, bots excluded.
#
# Three things this gets right that the obvious version does not:
#
# 1. The review list is the full HISTORY, so `select(.state == "APPROVED")`
#    alone answers "did anyone ever approve this". A reviewer who approved and
#    then pushed CHANGES_REQUESTED still matches. Group by reviewer and take
#    their last verdict instead.
# 2. COMMENTED is dropped BEFORE grouping: leaving a comment after approving
#    does not withdraw the approval, so it must not displace it.
# 3. Bots are excluded on `.user.type`, which is why this uses `gh api` and not
#    `gh pr view --json reviews` -- that form reports no type at all and
#    strips the `[bot]` suffix from the login, so Copilot's review arrives
#    indistinguishable from a person's. A Copilot approval is not acceptance.
#
# REST returns reviews oldest-first and jq's sort is stable, so `last` really
# is each reviewer's most recent verdict.
gh api "repos/<owner/repo>/pulls/<N>/reviews" --paginate --jq '
  [ .[]
    | select(.user.type != "Bot")
    | select(.state != "COMMENTED")
  ]
  | group_by(.user.login)
  | map(last)
  | map(select(.state == "APPROVED") | .user.login)
  | unique | join(", ")'
```

- A non-empty result → flip the status to `Accepted` and record the approver,
  in the ADR's own flavor. ol-infrastructure: `**Status:** Accepted`, a Review
  History row naming them, and a bumped `**Last Updated:**`. witan: there is no
  Review History table and no `**Last Updated:**`, so it goes on the status
  line — `- Status: Accepted 2026-10-14 by @handle` — the same place that
  flavor records amendments.
- Empty (admin merge, self-merge, no review) → it stays `Proposed`, and the
  skill **says so out loud** rather than leaving it to be discovered.

This is a mechanical edit driven by an observable event, not an agent forming a
judgment about whether a decision is good — which is what the humans-only rule
actually guards against.

**The edit needs its own pull request.** The acceptance event is the merge, so
the edit necessarily comes after it: the file being changed is already on the
default branch. A local edit does not update it, and neither does a push to the
merged branch — that branch is spent. Without a PR to carry it, the record on
`main` stays `Proposed` while everyone involved believes it was accepted, which
is the exact drift this whole section exists to close.

So acceptance ships as one small follow-up PR — `docs(adr): accept ADR-0014
after #412` — carrying the status flip, the approver, and any index or docs
regeneration the repo requires. It gets no plan comment and no task: it is
bookkeeping the merge triggered, not new work.
[`implement-plan`](../../implement-plan/SKILL.md) opens it at close-out.

**Staleness, with teeth.** At close-out, any ADR that has been `Proposed` for
more than 90 days while the paths it governs have shipped code is worth one
line of output: *these claim to be undecided and are in production*. One line,
at a moment someone is already reading output, beats another scheduled review
nobody runs. [`complete-project`](../../complete-project/SKILL.md) Step 6
reports it.

## Revisions: an accepted ADR is never edited

**There is one revision mechanism, and it is supersession.** An ADR that has
been accepted is immutable. When the decision it records changes — at all, by
any amount — write a new record that supersedes it. Do not append an addendum,
do not add an amendment, do not rewrite `## Decision`, and do not "clarify" it.

This is the policy `docs/context/08-architecture-decisions.md` in
ol-infrastructure already states: *"Don't edit the original ADR — ADRs are
immutable once accepted"* and *"Create a new ADR that supersedes the old one."*

The reason is what a reader has to do. An amended record can only be understood
by replaying its history: read `## Decision`, then each dated edit, and
reconstruct what is currently true from the sequence. Every reader pays that
cost, every time, forever — and the ones who skip to `## Decision` and stop
reading get an answer that is wrong. A superseding record states current truth
once, in one place, and the old one says where to go.

### Existing records still carry addenda — read them

This forbids **writing** them. It does not erase the ones already in the two
repos, and they are not rare: ol-infrastructure ADR-0009 carries three
`## Addendum` sections plus a `## Resolution`, and witan ADRs 0001, 0003, 0004
and 0011 carry eight `## Amendment` sections between them. Several are
`Accepted`.

So when reading an ADR, read it **whole** — `## Decision`, `## Consequences`,
and every `## Addendum` or `## Amendment` on it. On those records the original
wording is routinely the stale half, and taking the decision from the top of
the file is how you implement against something that was revised two years ago.

Do not retrofit them. Converting a historical addendum into a supersession
rewrites a record of what people knew and when, which is the one thing the
immutability rule is protecting. They stay as they are; new changes go the new
way.

## When a superseding record gets written

**At project close-out. Only there.** One trigger, no mid-flight exceptions —
run by [`complete-project`](../../complete-project/SKILL.md).

Every ADR the project's deviations touched gets one superseding record,
covering everything that moved — written once the work is done and the decision
has stopped moving.

Nothing is written to any ADR while the work runs. A deviation goes in
[the deviation log](../../plan-technical-approach/references/deviation-log.md)
and stays there until the end.

**Why not sooner.** Superseding per deviation, or at a two-deviation threshold,
chains records: supersede on Tuesday, and Thursday's deviation supersedes the
Tuesday one. A decision that moved four times yields four ADRs, three of them
already dead on arrival, and a reader has to walk the chain to find the live
one — the stacked-edits problem again, with numbers attached. Waiting until the
decision has settled means **one record per decision per project**, stating
what it finally became rather than what it was becoming.

**The cost, which is real.** Between a deviation and close-out, the ADR is
stale and anyone reading it gets the superseded answer. This is accepted
deliberately: for that window, the deviation log is where current truth lives,
and `implement-plan` reads the log alongside the governing ADRs for exactly
that reason. The window closes when the work does, and it never spans projects.

**Cite the deviation log's commit**, because that log is deleted at close-out.
It is consumed, not archived:

```markdown
Derived from the deviation log at `a3f91c2`.
```

Get the sha with `git rev-parse --short HEAD` *before* the `git rm`. Without
it the superseding record asserts that a decision changed and leaves no way
back to the entries that say how — and an unanchored reference to a deleted
file is worse than none, because it reads as though it still resolves.

Superseding is bidirectional and leaves both files in place:

1. Write the new ADR with its own number. Its `## Related Decisions` says
   `Supersedes ADR-NNNN`.
2. On the old one, set `**Status:** Superseded by ADR-MMMM` and add a
   `**Superseded By:**` line.
3. Explain in the new ADR *why the decision changed* — not just what the new
   one is. That is the part a reader comes back for.

**Step 2 is the one permitted write to an accepted record, and it is not an
exception to the rule.** The status line is a pointer, not part of the
decision: it says *this is no longer current, go here*. Nothing in
`## Context`, `## Decision`, `## Options Considered` or `## Consequences` may
change. Without that pointer a superseded ADR stays silently authoritative to
anyone who finds it first, which is worse than either option this protocol
chooses between — the record is wrong AND nothing says so.

The same goes for the status flip from `Proposed` to `Accepted` above: an
accepted record is immutable, and acceptance is the event that makes it one.

Never delete a superseded ADR, and never renumber to close the gap. Numbers are
permanent.
