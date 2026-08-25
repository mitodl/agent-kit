# Walkthrough: Agent-driven

Same task, but now you're inside a running agent session (Claude Code, Pi,
whichever). Nobody types a `/` command for any of this — the agent calls MCP
tools because the work in front of it calls for them, the same way it decides
to open a file or run a test.

You say:

> Pick up `tk-retry-logic-drops-the-last-attempt-s-e-4f9c21` and fix it.

## The agent claims before touching anything

Because `AGENTS.md`/`CLAUDE.md` instructions (or the `witan-task` skill's own
guidance, which the agent has read even without you invoking it) say a task
gets claimed before the first edit, it calls:

```python
task_claim(slug="tk-retry-logic-drops-the-last-attempt-s-e-4f9c21", assignee="claude-session-8f21")
# → {"claimed": true, "status": "in_progress"}
```

If that had come back `{"claimed": false, "held_by": "someone-else"}`, the
agent's next move is to tell you and stop — not to barrel ahead and clobber
someone else's claim.

## It checks what's already known

Before writing a fix, a well-behaved agent checks whether this has come up
before:

```python
recall(query="retry loop exception handling", task="tk-retry-logic-drops-the-last-attempt-s-e-4f9c21")
```

`recall` seeds from both the query and the task, expands a hop across any
linked memories, and comes back empty here — nobody has hit this before. If it
hadn't been empty, the agent would read the existing lesson before writing
code that repeats it.

## It finds the second bug on its own

Reading the retry loop to fix the reported issue, the agent notices the
`CancelledError` problem too — and files it without being asked, because
losing a follow-up mid-task is the exact failure `discovered_from` exists to
prevent:

```python
task_create(
    title="Retry loop swallows CancelledError",
    type="bug",
    priority="p2",
    discovered_from=["tk-retry-logic-drops-the-last-attempt-s-e-4f9c21"],
)
# → {"slug": "tk-retry-loop-swallows-cancellederror-b81a02"}
```

## It writes the fix, then records the lesson

After the fix and a passing test:

```python
task_close(
    slug="tk-retry-logic-drops-the-last-attempt-s-e-4f9c21",
    resolution="Re-raise the final exception instead of swallowing it; test added",
)

memory_store(
    kind="lesson",
    title="Retry loop must re-raise the last attempt's exception",
    content="The final exception in a retry loop was being discarded, making a permanent failure look like a plain timeout to the caller. Re-raise on the last attempt.",
    tags=["retry", "error-handling"],
)
# → {"slug": "les-retry-loop-must-re-raise-…"}
```

This is the step [CLI-driven](cli-driven.md#the-thing-the-cli-cant-do)
couldn't do — an agent is *always* the one storing memory, whether it got
there by your instruction, a skill's script, or its own judgment mid-task.
That's the whole distinction this page is drawing: the same `memory_store`
call, made because the agent decided to, not because a script told it to ask
you first.

---

**Next:** [Skills-driven →](skills-driven.md)
