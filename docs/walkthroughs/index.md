# Walkthroughs

One scenario, worked through three times — once for each of the [three ways
in](../concepts/interfaces.md). Same task, same follow-up discovery, same
lesson worth keeping; only the caller changes.

## The scenario

Someone already filed the bug from [Tasks and
projects](../getting-started/tasks-and-projects.md#file-a-task):

```
tk-retry-logic-drops-the-last-attempt-s-e-4f9c21
  "Retry logic drops the last attempt's error"
  bug · p1 · open
```

It's your turn to pick it up. Along the way you'll notice a second, related
bug, and want to leave something behind for the next person who touches this
code.

<div class="grid cards" markdown>

-   **[CLI-driven](cli-driven.md)**

    A person, at a terminal, no agent session running. Claim, fix, close —
    and the one thing the CLI flatly cannot do for you.

-   **[Agent-driven](agent-driven.md)**

    Inside an agent session, tools called directly as part of the work — no
    slash command in sight.

-   **[Skills-driven](skills-driven.md)**

    The same session, but reaching for `/witan-task` and letting it drive the
    claim-and-triage decisions.

</div>

## What actually differs

| | CLI-driven | Agent-driven | Skills-driven |
| --- | --- | --- | --- |
| Who initiates each step | You, every time | The agent, on its own judgment | You, via a slash command; the agent follows a script |
| Can store a memory? | No — `witan memory` only reads | Yes — `memory_store`, on the agent's own judgment | Yes, same as agent-driven — `/witan-task`/`/witan-workflow` don't call `memory_store` themselves, but the agent still can mid-session |
| Picking which task to claim | You already know the slug | The agent reads it from context, or asks | `/witan-task` shows a picker |
| Best for | Triage, scripting, a terminal you already have open | Work happening inside a normal agent session | The moments easy to get wrong by hand — claiming, session hand-off |

None of these are exclusive. A real session usually mixes all three — see the
close of [Three ways in](../concepts/interfaces.md#picking-one) for how they
typically layer.
