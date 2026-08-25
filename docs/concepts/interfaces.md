# Three ways in: CLI, agent, skills

Every witan operation is ultimately a function call against the graph. Three
different things can make that call, and knowing which one you're using — or
which one a doc page is describing — matters, because they don't all have
access to the same operations and they don't all involve a human at the same
point.

<div class="grid cards" markdown>

-   **CLI — you, at a terminal**

    Typing `witan tasks`, `witan memory "…"`, `witan run tk-…`. Read-heavy,
    scriptable, no agent required.

-   **Agent — direct MCP tool calls**

    Your coding agent calling `task_create`, `memory_store`, `recall` as part
    of its own reasoning, because it decided to, not because you ran a
    command.

-   **Skills — a guided, interactive script**

    You invoke `/witan-task` or `/witan-workflow`; a packaged set of
    instructions asks you questions and makes the MCP calls on your behalf.

</div>

## CLI: for a human, not for an agent

The `witan` binary is what you type. It's good for triage, browsing, and
anything you'd rather see in a terminal than have summarized back to you —
`witan tasks --ready`, `witan memory "flaky retry"`, `witan project status
wp-…`.

**It is deliberately not a full interface to the graph.** There is no `witan
memory store` command — writing a memory is something an agent does when it
learns something, and the CLI has no comparable moment to hang that on. There
is no `witan code find-definition` either: code-graph queries return rows
meant to be reasoned over, not read on a screen. The CLI covers what a person
sitting at a keyboard actually wants to do directly.

Where the CLI and the MCP tools both offer an operation, **they are the same
implementation** — `witan tasks` calls the identical function `task_list`
exposes over MCP, then formats the result. They cannot disagree about what the
graph says, because there's only one code path to disagree with itself. See
[Architecture](../explanation/architecture.md#the-cli-is-not-a-second-implementation)
for why that's structural rather than a coincidence of the current code.

## Agent: tool calls without a human step in between

Once witan's MCP server is registered with your agent platform, the agent can
call any of its ~60 tools directly, in the middle of ordinary work, with no
slash command and no CLI involved. This is the mode that makes witan a
*shared* graph rather than a personal notebook: `AGENTS.md`/`CLAUDE.md`
instructions (or the agent's own judgment) tell it to check `recall` before
starting work, or to `memory_store` a lesson after fixing something
non-obvious, and it just does — the same way it decides to read a file or run
a test, without you typing a command for each one.

This is also where most task and memory *writes* actually originate. A person
files a bug with the CLI or a skill; an agent mid-task discovers a second bug
and calls `task_create(discovered_from=[...])` on its own, because that's what
"don't lose follow-up work" means in practice.

## Skills: interactive, but scripted

A skill (`/witan-task`, `/witan-workflow`, `/witan-memory`,
`/witan-project-tracker`) is a packaged set of instructions — not a program,
a `SKILL.md` file the agent reads and follows. Invoking one is still "the
agent calling MCP tools", but with a human back in the loop for the decisions
that shouldn't be made silently: which of several ready tasks to claim, what
this session's phase is, what to write in a hand-off summary.

Skills exist for the operations that are easy to get wrong by skipping a step
— claiming before working, remembering to end a session, picking the right
project when several are active — by turning "the agent should do the right
sequence of calls" into "the agent follows a script that has the sequence
built in." `/witan-task`, for instance, is also the thing to reach for
whenever you're about to start *any* `tk-` task, not only ones you found
through the skill itself, because claiming before the first edit is the one
step everything else depends on.

## Picking one

| You want to… | Reach for |
| --- | --- |
| Skim ready work, check a task's status, browse from a terminal | CLI |
| Let the agent record what it just learned, without asking you | Agent (direct MCP calls) |
| Pick a task to claim, or wire this session to a project, with prompts | A skill |
| Script something (CI, a cron job, a report) | CLI |
| Do the *same* thing a skill does but from an already-running session that skipped the slash command | MCP tools directly — `task_claim`, `task_close`, etc. |

They're not mutually exclusive within one piece of work. A typical session
might start with `/witan-workflow` (skill) to link the session, then have the
agent call `recall` and `memory_store` on its own several times (agent), while
you check progress with `witan tasks` in a second terminal (CLI). See
[Walkthroughs](../walkthroughs/index.md) for the same scenario worked through
each way end to end.
