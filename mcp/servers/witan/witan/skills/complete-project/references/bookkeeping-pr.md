# The bookkeeping pull request

One small pull request carrying every edit close-out makes.

## Why it has to exist

Close-out runs **after** the merge. Every file it touches — a status flipped to
`Accepted`, a superseding ADR, a deleted deviation log, a dropped
`applies_to` note, a regenerated index — is already on the default branch.

Neither obvious shortcut works:

- **Editing the working tree** changes nothing anyone else sees.
- **Pushing to the merged chunk's branch** changes nothing either. That branch
  is spent; its PR is closed.

Without a PR to carry them, the ADR stays `Proposed` on `main` while the task
and the session close around it — precisely the drift close-out exists to end.

## What it is

One PR per close-out, titled for what it does:

```text
docs(adr): accept ADR-0014 and supersede ADR-0011 after #412
```

It carries:

- Superseding ADRs written in Step 2
- The `git rm` of the deviation log
- Status flips to `Accepted`, with approvers recorded
- Dropped or corrected `applies_to` notes
- Whatever the repo regenerates — this one has a generated docs mirror and a
  nav entry; run its docs check **before** opening the PR, not after

**No plan comment and no chunk task.** It is bookkeeping the merge triggered,
not new work, so it does not enter the planning flow.

**Deleting the log and writing the record that replaces it belong in the same
diff.** A reviewer then sees the consumption as one change and can tell nothing
was dropped quietly. Split across two PRs, the first reads as deleting a
record.

## The rule in reverse

**Do not hold a chunk's PR open waiting for its own ADR to be accepted.** The
approval that accepts the ADR *is* the approval that merges the PR, so the
status edit can only ever come afterwards. A chunk held open for it waits
forever.

## The final acceptance flip, and why it terminates

The superseding ADRs this PR introduces land as `Proposed` — nothing has
reviewed them yet. So close-out ends with one more acceptance pass, over **this
PR**, delivered as a last status-flip PR once it merges.

That sounds like an infinite regress and is not: the status-flip PR carries
**only status lines and no new ADR**, so it produces nothing further to accept.
The chain is two PRs deep, always.

Where the repo's flavor records the approver differs — an ol-infrastructure ADR
gets a Review History row and a bumped `**Last Updated:**`, a witan ADR carries
it on the `- Status:` line. The reviewer query, and the three ways the obvious
version of it gets the wrong answer, are in
[the revision protocol](../../write-adr/references/revision-protocol.md).

**No approving human review → it stays `Proposed`,** and that is reported
rather than quietly flipped. An agent accepting its own record is the thing the
humans-only rule exists to prevent.
