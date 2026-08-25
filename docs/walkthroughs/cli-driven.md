# Walkthrough: CLI-driven

You're at a terminal, in a checkout of `mitodl/agent-kit`. No agent session is
running — you're about to fix this one by hand.

## Find and claim the task

```bash
witan tasks --ready
```

Prints a table titled "Ready tasks — mitodl/agent-kit" — priority, status,
type, slug, title, and a few more columns — with
`tk-retry-logic-drops-the-last-attempt-s-e-4f9c21` in it.

```bash
witan task claim tk-retry-logic-drops-the-last-attempt-s-e-4f9c21
```

```
Claimed tk-retry-logic-drops-the-last-attempt-s-e-4f9c21 (assignee=tmacey)
```

This sets `in_progress` with a lease under your author name and refuses if
someone else already holds it (`--force` overrides, but see [what a claim
actually guarantees](../getting-started/tasks-and-projects.md#what-a-claim-actually-guarantees)
before reaching for it).

## Fix it, and file what you found

While reading the retry loop you notice it also swallows `CancelledError`,
which is a separate bug from the one you were sent to fix. File it before you
forget, linked to the task that surfaced it:

```bash
witan task create "Retry loop swallows CancelledError" \
  --type bug --priority p2 \
  --discovered-from tk-retry-logic-drops-the-last-attempt-s-e-4f9c21
```

```
Created task: tk-retry-loop-swallows-cancellederror-b81a02
```

`--discovered-from` writes the `DiscoveredFrom` edge — see [the task
graph](../concepts/graph.md#tasks-dependency-aware-hierarchical) for what that
buys you later.

## Close it out

```bash
witan task close tk-retry-logic-drops-the-last-attempt-s-e-4f9c21 \
  --resolution "Re-raise the final exception instead of swallowing it; test added"
```

```
Closed tk-retry-logic-drops-the-last-attempt-s-e-4f9c21
```

## The thing the CLI can't do

You just fixed a bug caused by a subtle assumption — the retry loop assumed
every exception on the last attempt was safe to discard. That's exactly the
kind of thing worth recording as a `lesson`, so nobody rediscovers it the hard
way.

**The CLI can't do this part.** There is no `witan memory store` command —
storing memory is deliberately agent-only, because the intended author of a
memory is the agent that just learned the thing, and there's no comparable
moment for a human typing at a prompt to hang it on (see [Three ways
in](../concepts/interfaces.md#cli-for-a-human-not-for-an-agent)). If you want
this fix on record, you need an agent in the loop for at least that one step —
which is exactly what the other two walkthroughs cover.

```bash
witan memory "retry" --kind lesson
```

still works, from the CLI, once someone (or something) has written it.

---

**Next:** [Agent-driven →](agent-driven.md)
