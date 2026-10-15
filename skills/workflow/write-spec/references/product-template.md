# PRODUCT.md template

The user-facing half of a spec written by [`write-spec`](../SKILL.md). It
answers what should happen and for whom, in language someone outside
engineering can check. No implementation detail — that is `TECHNICAL.md`'s job,
and mixing them means neither reader trusts the document.

## Writing rules

- **Plain sentences.** This is read by product, design, support and engineers
  who join in a year. Jargon that needs the codebase to decode belongs in
  `TECHNICAL.md`.
- **Acceptance criteria are checkable by someone who did not build it.** If
  verifying one requires reading the diff, it is a task, not a criterion.
- **Out of scope is required**, not a courtesy. Most spec arguments are about
  something one person assumed was included.
- **Open questions stay visible** until they are closed, with the answer
  written in rather than the question deleted. A question that vanishes looks
  answered when it was forgotten.
- Do not hard-wrap prose that will be read on GitHub.

## Template

```markdown
# <Title>

**Issue:** <full GitHub issue URL>
**Status:** draft | approved | shipped
**Last updated:** <YYYY-MM-DD, from `date -u +%Y-%m-%d`>

## Problem

<What is wrong or missing today, from the user's side. Concrete: what someone tries to do and what happens instead. Not "the search is suboptimal" — what does a person do, and what do they get?>

## Who it affects

<Which users, and roughly how many or how often. If it affects an internal team rather than end users, say which team and which workflow. A change affecting 4% of accounts gets a different answer than one affecting everyone.>

## User stories

- As a <role>, I want to <action>, so I can <outcome>.

## Acceptance criteria

- [ ] <Checkable by someone who did not build it.>
- [ ] <Include the empty, error and permission-denied cases — those are where "done" is usually wrong.>

## Out of scope

- <Something a reader might reasonably assume is included, and is not.>
- <Deferred work, with the issue URL if one exists.>

## Open questions

- [ ] <Question> — <who can answer it, and what is blocked until they do>
```

## Worked example

```markdown
# Saved-search notifications

**Issue:** https://github.com/mitodl/hq/issues/12184
**Status:** approved
**Last updated:** 2026-10-14

## Problem

A learner who saves a search has no way to hear about courses that match it later. They either re-run the search by hand every few weeks or miss the course — and the second is what actually happens. Support sees this as "why didn't you tell me about X", roughly twice a month.

## Who it affects

Learners who have saved at least one search: about 3,100 accounts today, concentrated in the ones who browse before an enrollment window opens. Also the support team, who currently answer the question manually.

## User stories

- As a learner, I want to be told when a new course matches a search I saved, so I can enroll before it fills.
- As a learner, I want to turn notifications off per saved search, so a broad search I use for browsing does not mail me weekly.

## Acceptance criteria

- [ ] A learner with notifications on for a saved search receives one email when a newly published course matches it.
- [ ] Several matches in the same day produce one email listing them, not one email each.
- [ ] A learner can turn notifications on and off per saved search, and the setting survives editing the search.
- [ ] Turning them off stops mail within the hour.
- [ ] A saved search matching nothing produces no mail at all — not an empty digest.
- [ ] Deleting a saved search stops its notifications and does not error on a queued send.

## Out of scope

- Any channel other than email. In-app and push are deferred.
- Notifying on course *updates* — this is new matches only.
- Back-filling for courses published before this ships; the first email covers courses published after.

## Open questions

- [x] Digest or immediate? — **Daily digest.** Immediate was rejected: a broad saved search can match a dozen courses in a bulk publish.
- [ ] Does this need its own unsubscribe, separate from the global marketing preference? — legal/compliance, blocks the email template.
```

Note the two things this example does that matter most: the acceptance criteria
name the empty, duplicate and deletion cases rather than only the happy path,
and the closed open question keeps its answer instead of being deleted.
