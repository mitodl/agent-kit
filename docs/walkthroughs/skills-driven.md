# Walkthrough: Skills-driven

Same session, same task — but instead of naming the slug yourself, you reach
for the packaged triage flow.

## Triage with `/witan-task`

```
/witan-task
```

The skill calls `task_ready()` for the current repo and asks you to pick, via
an interactive question rather than a wall of text:

```
Claim task
Which task do you want to work on?
  ○ Retry logic drops the last attempt's error
    [p1] slug: tk-retry-logic-drops-the-last-attempt-s-e-4f9c21
  ○ (other ready tasks…)
  ○ Create a task
  ○ None
```

You pick the first one. The skill claims it on your behalf:

```python
task_claim(slug="tk-retry-logic-drops-the-last-attempt-s-e-4f9c21", assignee="<your session id>")
```

and confirms: *"Claimed **Retry logic drops the last attempt's error**
(`tk-retry-logic-drops-the-last-attempt-s-e-4f9c21`). Close it with
`/witan-task close`, or `task_release` it if you step away."*

Nothing here is different under the hood from [the agent calling `task_claim`
directly](agent-driven.md#the-agent-claims-before-touching-anything) — the
skill is still an agent making an MCP call. What's different is that *which*
task to claim was a question put to you, not a decision the agent made alone.

## Fix it, then close through the skill

You (or the agent, mid-fix) notice the same `CancelledError` issue as before.
That part isn't skill-gated — filing a discovered task is ordinary agent
behavior, skill or not:

```python
task_create(
    title="Retry loop swallows CancelledError",
    type="bug", priority="p2",
    discovered_from=["tk-retry-logic-drops-the-last-attempt-s-e-4f9c21"],
)
```

Once the fix is in and tested:

```
/witan-task close
```

The skill asks which task and for a resolution note, then calls:

```python
task_close(
    slug="tk-retry-logic-drops-the-last-attempt-s-e-4f9c21",
    resolution="Re-raise the final exception instead of swallowing it; test added",
)
```

and offers to run `task_ready()` again so you can see what just unblocked.

## Where a second skill would come in

If this task were part of a multi-session effort rather than a one-off fix,
`/witan-workflow` at the start of the session would have asked which
`WorkflowProject` this work belongs to (or offered to create one), then called
`workflow_session_start` — the same re-entrant call [the task
graph](../concepts/graph.md#projects-an-objective-across-sessions) describes —
before any of the above. That's a second, independent skill: `/witan-task`
picks *what* to work on; `/witan-workflow` tracks *the session's place* in a
longer effort. A session can use either, both, or neither.

## Why bother, if it's the same calls underneath

Because the calls it's easy to skip by hand are exactly the ones a lease-based
system depends on. Claiming *before* the first edit only works if it actually
happens every time — a skill that asks "which task?" and then claims it as
part of answering removes the chance to start editing first and claim later
"once you remember." The [task manager skill's own
instructions](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/witan/skills/witan-task/SKILL.md)
are blunt about this: two sessions have already written the same fix for the
same task on the same day, each unaware of the other, because neither claimed
it first.

---

**Back to:** [Concepts: three ways in](../concepts/interfaces.md) · [Walkthroughs overview](index.md)
