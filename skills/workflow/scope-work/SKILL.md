---
name: scope-work
description: >
  Scope a piece of work before any of it gets built: establish or create the
  GitHub issue, ground the problem in the actual code, and decide how much
  process it deserves. Use this skill at the start of any feature or bug — when
  asked to "implement <feature>", "fix this bug", "work on issue #N", "start on
  <ticket>", "what's involved in", "how big is this" — or whenever work is
  about to begin without an agreed scope. Routes to one of three outcomes: a
  trivial change goes straight to planning, work that needs a design record
  gets the full treatment, and work that cannot be sized yet gets a time-boxed
  spike first. Tracks the work as a witan task. Requires the witan MCP server.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Scope the Work

**One output: a scoped issue and a routing decision.** This skill does not
design anything and writes no code. It answers "what are we actually doing, and
how much process does it deserve?" — then hands off.

```text
  scope-work
  |
  +-- trivial     ------+
  |                     |
  +-- decision(s) ------+--> plan-technical-approach --> implement-plan --> shipped
  |                              |
  |                              +--> write-adr
  |                                   one ADR per decision worth recording,
  |                                   and zero is a normal outcome
  |
  +-- unknowable  --> spike --> re-enter scope-work with the findings
```

A **trivial** change still goes through `plan-technical-approach`, but on its
fast path: one obvious approach, no alternatives, no scoring, and no ADR.

**Planning always comes before the record.** An ADR's `## Options Considered`
is the output of scoring alternatives, so `write-adr` is entered from
`plan-technical-approach`, never directly from here. What this skill decides is
*how much* planning the work deserves — not which artifact gets written.

| File | Purpose |
|------|---------|
| [`references/complexity-triage.md`](references/complexity-triage.md) | The three tiers, their criteria, and the spike protocol |
| [`references/witan-tracking.md`](references/witan-tracking.md) | Task lookup, claiming, project join-before-create |

## Prerequisites

Work is tracked in witan, so the `task_*` and `workflow_*` tools must be
connected. They ship with the witan MCP server, installed by `witan setup` —
not by `agent-kit apply`, so a consumer can have this skill without them. If
they are missing, say so, point at `witan setup` and
`mcp/servers/witan/README.md`, and stop.

---

## Step 1 — Planning posture

Nothing is edited and nothing is written to GitHub or the task graph until the
issue is established and the route is chosen. If the platform provides a plan
mode (Claude Code's, exited with `ExitPlanMode`), use it; otherwise hold the
same rule by hand.

The one sanctioned write before the route is chosen is creating an issue in
Step 2, which carries its own confirmation gate inside the skill that does it.
Claiming the task in Step 5 is the other, and by then the scope is settled.

---

## Step 2 — Establish the issue

Resolve the target in this order, and never re-ask for something already known:

1. An explicit URL or `#N` in the request.
2. An issue already established earlier in this conversation.
3. `task_for_branch(branch="<current branch>")` — a `WorksOn` edge is written
   only by a successful `task_claim`, so a hit means someone really is working
   this branch, and its `external_uri` is the issue.
4. `gh issue list --repo <owner/repo> --assignee @me --state open`.
5. Ask.

**No issue yet?** Hand off to
[`create-ol-github-issue`](../../process/create-ol-github-issue/SKILL.md). Pass
what this session already knows — the repo from the git remote, and Technical
Issue as the default template for engineering work — so the engineer is not
re-asked three things. Come back at Step 3.

**Acceptance criteria belong in the issue body.** If the issue has none, add
them there — not to the plan comment later. The independent review in
[`create-ol-pull-request`](../../process/create-ol-pull-request/SKILL.md) takes
goals only from sources the authoring session did not write; criteria that live
in the agent's own plan comment are self-review with extra steps.

Read the whole issue, not the title:

```bash
gh issue view <N> --repo <owner/repo> --json title,body,labels,comments,url
```

Comments routinely carry the constraint that invalidates the obvious approach.

**Tracker repos.** In `mitodl/hq` the issue lives in the tracker while the code
lives in a product repo;
[`github-issue-triage`](../../process/github-issue-triage/SKILL.md) explains
how to resolve which. Record both — they are used differently downstream: the
plan comment goes on the repo holding the **issue**, the code and any ADRs live
in the **product** repo, and the task lookup is scoped to the product repo
while matching on the issue URL.

---

## Step 3 — Ground the problem in the code

Read the code before classifying anything. Triage done off the issue title is
guessing, and the whole value of this skill is that the routing decision is
evidence-backed.

Where the code graph is available, `code_find_definition` gives a `symbol_id`
and `code_impact` gives the blast radius; otherwise use `grep` and `git log -S`.
Either way establish three things: where the behavior lives now, what calls it,
and what a change there would touch.

**Record a measured baseline, not adjectives.** Caller counts and touch-points
are what the triage in Step 4 rests on, and what the sizing and scoring in
`plan-technical-approach` consume later. A number here saves re-deriving it
twice.

Check what is already known: `recall` / `memory_search` for a stored lesson or
project fact about this area, and `symbol_context(symbol_id)` for tasks and
memories already attached to the code.

---

## Step 4 — Classify and route

Decide with Step 3's evidence in hand. The tiers, their full criteria, and the
anti-abuse framing are in
[`references/complexity-triage.md`](references/complexity-triage.md):

| Tier | Route |
|---|---|
| **Trivial** — one obvious mechanism, contained, tens of lines, plain revert undoes it, not auth/payments/PII | [`plan-technical-approach`](../plan-technical-approach/SKILL.md), **fast path**: one approach, no scoring, no ADR |
| **Has a decision worth recording** — anything else that is knowable | [`plan-technical-approach`](../plan-technical-approach/SKILL.md), **full path**: alternatives scored, then [`write-adr`](../write-adr/SKILL.md) records what was decided |
| **Unknowable** — a load-bearing fact is missing, so it cannot be sized | Spike first, then re-enter here |

**When in doubt, it is not trivial.** Running the full process on a simple
change costs ten minutes. Skipping it on a complex one ships an unexamined
design decision. Classify conservatively.

Say which tier you chose and which criteria decided it, so the engineer can
disagree cheaply before any of the downstream work starts.

**Off-ramp: the issue may be wrong.** Grounding regularly turns up a false
premise, work that already landed, or another repo's problem. Do not route work
that should not happen. Report it, reuse the verdict vocabulary in
[`github-issue-triage`](../../process/github-issue-triage/SKILL.md) rather than
inventing one, and record it with `task_comment(slug=..., text=...)` — not
`task_update`, which overwrites someone else's description, and not
`task_create`, since "your premise is wrong" is not work.

---

## Step 5 — Track it

Find the task, create it only if there is none, claim it, and join or create
the project at phase `discovery`. The lookup is not a one-call affair — the
chain, the claim semantics and the project rules are in
[`references/witan-tracking.md`](references/witan-tracking.md).

Claim before handing off. An unclaimed task being actively worked is
indistinguishable from an idle one, and a second session will start the same
work.

---

## Step 6 — Hand off

Say explicitly which skill comes next and what it is being given: the issue
URL, the product repo, the measured baseline from Step 3, and the tier with its
justification. The next skill should not have to re-derive any of it.

A spike route is different — it ends here. Create the spike task as described
in [`references/complexity-triage.md`](references/complexity-triage.md), run it
or hand it off, and re-enter this skill at Step 3 once the question is
answered, because the findings change the grounding the triage rests on.
