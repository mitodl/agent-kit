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

**Staleness, with teeth.** When `implement-plan` runs, any ADR that has been
`Proposed` for more than 90 days while the paths it governs have shipped code
is worth one line of output: *these claim to be undecided and are in
production*. One line, at a moment someone is already reading output, beats
another scheduled review nobody runs.

## Revisions: addendum by default

**Append a dated addendum. Do not rewrite the decision.**

```markdown
## Addendum (2026-10-28) — the digest is assembled by Celery beat, not cron

The decision stands; the mechanism named in `## Decision` does not. This
service has no cron infrastructure, and adding it for one job was a larger
change than the feature. <What changed, and the fact that was not known when
the decision was made.>
```

Then add an `Amended` row to the Review History and bump `**Last Updated:**`.
In the witan flavor, extend the `- Status:` line instead —
`Accepted, amended 2026-09-22 (...)`.

Append; never edit or delete an existing addendum. A change later reversed gets
a second addendum saying so.

### This contradicts the written policy, deliberately

`docs/context/08-architecture-decisions.md` in ol-infrastructure says *"Don't
edit the original ADR — ADRs are immutable once accepted"* and *"Create a new
ADR that supersedes the old one."* The skill follows **observed practice**
instead, and should say so in the pull request so a reviewer who has read that
guide is not surprised:

- **Twelve in-place revisions across the two repos**, counted: ol-infrastructure
  ADR-0009 carries three `## Addendum` sections plus a `## Resolution`, and
  witan ADRs 0001, 0003, 0004 and 0011 carry eight `## Amendment` sections
  between them. Several of those ADRs are `Accepted`.
- **No ol-infrastructure ADR has ever been superseded.** The string
  "Superseded" appears there only in `template.md` and `README.md`, in no
  actual ADR — the status exists on paper and has never been used.

The one real supersession in either repo is *partial and cross-repo*: witan
ADR-0004 carries `- Supersedes: ADR-0009 (ol-infrastructure) D3's
per-team-token assumption`. It replaces one assumption inside another repo's
ADR rather than the whole decision, and it did not flip that ADR's status —
which is itself worth noting, since the documented protocol says supersession
is bidirectional.

A policy with one partial, cross-repo instance and a practice with twelve is
not a close call. The
upstream ADR convention explicitly permits this: *"In theory, immutability is
ideal. In practice, mutability has worked better for our teams… with a date
stamp, and a note that the info arrived after the decision."*

## When to supersede instead

Supersede when the **decision itself is reversed** — the chosen mechanism is
abandoned for a different one. Not when a detail moves.

The distinction: if `## Decision` would still be broadly true with an addendum
attached, amend. If it would be false, supersede.

Superseding is bidirectional and leaves both files in place:

1. Write the new ADR with its own number. Its `## Related Decisions` says
   `Supersedes ADR-NNNN`.
2. On the old one, set `**Status:** Superseded by ADR-MMMM` and add a
   `**Superseded By:**` line.
3. Explain in the new ADR *why the decision changed* — not just what the new
   one is. That is the part a reader comes back for.

Never delete a superseded ADR, and never renumber to close the gap. Numbers are
permanent.
