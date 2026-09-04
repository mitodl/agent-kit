# Why the witan-code inject-context block is not acted on

Task: `tk-diagnose-why-the-witan-code-inject-context-block-440e8d`
(project `wp-witan-enhancements-41e474`). Measured 2026-09-03 over
100 agent-kit sessions, 2026-08-01 → 2026-09-03 — the first window long
enough to bound the effect of PR #163, and entirely after it.

## The question

PR #163 rewrote the `witan-code inject-context` block to lead with the
`ToolSearch` that makes the `code_*` tools callable, on the theory that a
preference for tools absent from the tool list is not actionable. A
1.5-day re-measurement afterwards found 0 `code_*` calls and 0 `code_`
`ToolSearch` queries in 66 injections — too small a sample to conclude
anything, but enough to reject "add more block text" as the next move.

Two questions, then: does the line work over a real window, and if not,
what actually stops it?

## Answers

**No detectable change in session-level uptake.** The fraction of sessions
that touch a `code_*` tool is 5.9% before and 6.0% after (Fisher p = 1.0),
and the attempt rate the rewrite targeted is 1.02% vs. 0.84% per injection
(p = 0.79). Point estimates, not a demonstrated decrease. What the window
*does* establish is a ceiling: the 95% interval on post-#163 adoption tops
out at 12.5%, so whatever #163 bought, it was not the step change the
rewrite was aimed at.

**Deferral is not the binding constraint.** `code_*` and `task_*` arrive
deferred in the same 97 of 100 sessions, at the same `ToolSearch` cost.
89 of those sessions pay that cost for `task_*` and 6 for `code_*`.

**The binding constraint is substitution.** The tools that get loaded are
the ones with no Bash substitute. There is no way to claim a task with
`rg`; there is an obvious way to find a definition with it, and the agent
runs 6,432 of those.

## Method

Walk `~/.claude/projects/*agent-kit*/*.jsonl`, count block injections by
section heading in user/attachment records, and count `tool_use` entries
recursively anywhere in each record. Two traps, both of which silently
report zero:

- MCP tools appear as `mcp__witan__code_*`, never bare `code_*`. Matching
  the bare prefix finds nothing.
- The deferred-tool list is `attachment.addedNames`, a JSON **list** of
  names, not prose. A text scan of the record misses it.

A third caveat is not a trap but a limit: `~/.claude/projects` retention
is ~30 days, so the pre-#163 July transcripts no longer exist. The
pre-#163 column below is quoted from the measurement recorded at the time
(memory `pf-code-uptake-after-pr-163-the-inject-context-bloc-5c7cf6`) and
cannot be recomputed. The block also was not static across the window:
PR #291 (2026-08-28) added the unreadable-store branch.

## What the numbers say

| | pre-#163 (51 sessions) | post-#163, 1.5 days (7) | post-#163, 34 days (100) |
| --- | --- | --- | --- |
| Code Graph injections | 586 | 66 | 951 |
| `code_*` calls | 5 | 0 | 17 |
| `ToolSearch` mentioning `code_` | 6 | 0 | 8 |
| sessions using a `code_*` tool | 3/51 (5.9%) | 0/7 | 6/100 (6.0%) |
| `code_*` deferred | 51/51 | 7/7 | 97/100 |

Every pre/post difference in that table is within noise, and the honest
reading is a ceiling rather than a verdict:

| comparison | pre | post | Fisher (2-sided) |
| --- | --- | --- | --- |
| sessions adopting a `code_*` tool | 3/51 (5.9%) | 6/100 (6.0%) | p = 1.00 |
| `ToolSearch` for `code_` per injection | 6/586 (1.02%) | 8/951 (0.84%) | p = 0.79 |
| `code_*` calls per injection | 5/586 (0.85%) | 17/951 (1.79%) | p = 0.18 |

Calls per injection have the largest point estimate — roughly double — and
still do not separate; the whole rise is two sessions using the tools more,
which is why sessions-that-adopt is the metric to track and calls-per-
injection is not. The 95% Wilson interval on post-#163 adoption is
[2.8%, 12.5%]: a large improvement is excluded, a small one is not, and no
decrease is supported. Either way the line PR #163 added is issued about
twice in a thousand injections.

The two findings this diagnosis actually turns on are not close calls.
89/100 vs. 6/100 sessions loading `task_*` vs. `code_*` is p < 0.001; 674
definition-shaped `rg` invocations against 3 `code_find_definition` calls
needs no test at all. The pre/post comparison is the weak part of this
document and the mechanism does not rest on it.

### Deferral costs nothing when there is no substitute

Every witan tool family is deferred identically. Uptake is not:

| family | deferred in | sessions that `ToolSearch` for it | queries |
| --- | --- | --- | --- |
| `task_*` | 97/100 | 89 | 225 |
| `workflow_*` | 97/100 | — | 171 |
| `memory_*` | 97/100 | — | 72 |
| `code_*` | 97/100 | 6 | 8 |

Same channel, same round-trip, ~28x difference in how often it is paid.
Whatever stops `code_*`, it is not the cost of loading it.

### The competitor is `Bash(rg …)`, not the Grep tool

Across 100 sessions and 27,277 tool calls:

| | calls |
| --- | --- |
| `Bash` | 19,180 |
| — of which invoke `rg`/`grep`/`find`/`fd` | 6,432 (94/100 sessions) |
| — of which are definition lookups (`rg … def\|class`) | 674 |
| `Grep` tool | **0** |
| `Glob` tool | **0** |
| `code_find_definition` | 3 |

Two things follow. The block's "use them instead of grep" names a
behavior that does not occur — the Grep *tool* is never called — while the
habit it means to displace lives in `Bash` and is reinforced by a standing
system-prompt instruction ("Prefer `rg` (ripgrep) over `grep` for code
search"). And the ratio on the one comparison the block is about is
674 : 3, or 0.4%.

### The tools that do get used are the ones `rg` cannot answer

All 17 calls:

| tool | calls | Bash substitute? |
| --- | --- | --- |
| `code_indexed_repos` | 6 | no |
| `code_repo_dependencies` | 3 | no |
| `code_interface_search` | 3 | no |
| `code_find_definition` | 3 | yes (`rg 'def X'`) |
| `code_indexed_branches` | 2 | no |
| `code_callers` | **0** | roughly |
| `code_impact` | **0** | roughly |

14 of 17 are inventory or cross-repo questions with no local answer. The
two tools the block's own call template names as the payoff —
`code_callers` and `code_impact` — were called zero times in 951
injections. The advertised path is the least-used part of the surface.

### Loading the tools does not produce a use

Two sessions issued the block's own query verbatim or near-verbatim
(`+code_ find_definition callers impact`). Both made zero `code_*` calls
afterwards; one was diagnosing a witan outage in which no witan tool was
indexed at all, the other loaded the tools and went straight back to
`task_*`. Every real `code_*` use came from a `select:`-form query the
agent composed itself, *after* forming a specific question the graph
alone could answer ("is this repo indexed?", "what consumes `BASE_URL`?").
So attempt rate is not the bottleneck either: the trigger is the question,
and the block cannot supply one.

### When the advertised path was exercised, it mostly failed

One session (2026-08-25) genuinely tried symbol navigation. Of its 8
`code_*` calls: three returned an omnigraph schema error (a store written
by 0.8.x, read by 0.10.0), one returned `{"result": []}` with no
explanation, one blew the tool-output token limit at 70,592 characters,
and three succeeded — the last only after the agent added `branch="main"`
to a lookup that had failed without it earlier in the session. That
session was working on the stale-store bug, so it is a biased sample, and
PR #291 has since made an unreadable store say so. It still shows the
shape of the reinforcement: the substitute never errors, never returns a
silent empty, and never needs a second guess at an argument.

## The mechanism

An agent loads a deferred tool when it has already formed a question that
tool uniquely answers. It does not load one from an advertisement.
Deferral is free when nothing else can answer (`task_*`, `workflow_*`,
`memory_*`: 89% uptake) and decisive when something can, because the
substitute is already loaded, always works, and is named approvingly by a
higher-authority standing instruction.

This retires the leading hypothesis in the task — that witan-council's
In-Flight Branch section outperforms because it names a specific thing
rather than a capability. That comparison is confounded. 75 of 100
sessions began with a prompt that already named a task slug or
`task_claim`, and 53 of those claimed. Among the 25 sessions whose prompt
named no task, 25 saw a Ready Tasks block and 5 acted on it — 20%
(95% CI [8.9%, 39.1%]), not 59%. That subgroup is small enough that the
20% itself is loosely pinned; what it does establish is that the headline
59% cannot be read as the block's own effect, because three quarters of
those claims follow a prompt that already named the slug. Specificity
helps some; it is not the asymmetry it appeared to be.

## What follows

Not a rewrite. The block's exhortation demonstrably does not convert, so
spending more of the 600-char budget (`test_inject_context_block_stays_small`)
on better wording is spending against a mechanism that is not wording.
Three things do follow from the finding, in rough order of expected value:

1. **Pitch the no-substitute surface.** Every voluntary use was a
   cross-repo or inventory question. Symbol lookup competes with `rg`
   and loses 674:3; `code_interface_search` and `code_cross_repo_impact`
   compete with nothing. If the block advertises anything, it should
   advertise those, and it can then be shorter, not longer.
2. **Undefer `code_find_definition`.** This is the only intervention that
   changes the competition rather than the advertisement: at 1 call vs.
   1 call the block's claim becomes actionable, and it is a falsifiable
   test of the substitution hypothesis rather than another rewrite.
3. **Fix what punishes the agent that tries.** A `code_interface_search`
   that returns 70,592 characters is unusable in the tool loop regardless
   of what the block says. And a `code_find_definition` that answers a
   bare `{"result": []}` is indistinguishable from "no such symbol"; the
   one observed instance was on a feature branch minutes after a store
   rebuild, which is worth confirming as a branch-view gap rather than a
   coincidence — but either way an empty result needs to say which it is.
   Worth checking against `_resolve_branch`'s fall-through to main.

## Reproducing

Not committed as a script — it reads `~/.claude/projects`, which is outside
the repo and has no CI home. This is the whole rig; paste it and change the
glob to widen the repo set.

```python
import collections, glob, json, os

def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)

tot, sess = collections.Counter(), collections.Counter()
for f in glob.glob(os.path.expanduser("~/.claude/projects/*agent-kit*/*.jsonl")):
    seen = collections.Counter()
    for line in open(f, errors="replace"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        # Deferred arrivals are a LIST of names, not prose.
        att = rec.get("attachment") or {}
        if att.get("type") == "deferred_tools_delta":
            for family in ("code_", "task_"):
                if any(family in n for n in att.get("addedNames") or []):
                    seen["deferred_" + family] += 1
        blob = json.dumps(rec)
        for section in ("## Code Graph", "## Ready Tasks", "## In-Flight Branch"):
            if section in blob and rec.get("type") in ("user", "attachment", "system"):
                tot[section] += 1
        for d in walk(rec):
            if d.get("type") != "tool_use":
                continue
            name = d.get("name") or ""
            # MCP tools are mcp__witan__code_*, never bare code_*.
            tot[name] += 1
            if "code_" in name:
                seen["code"] += 1
            if name == "ToolSearch" and "code_" in json.dumps(d.get("input") or {}):
                seen["ts_code"] += 1
    sess["n"] += 1
    for k, v in seen.items():
        sess[k] += v > 0

print(sess)
print(tot.most_common(30))
```
