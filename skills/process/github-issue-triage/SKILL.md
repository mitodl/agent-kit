---
name: github-issue-triage
description: >
  Audit open GitHub issues to identify which are outdated, already completed, or
  superseded by newer issues — using parallel subagents to cross-reference issue
  descriptions against the current codebase and git history. Use this skill when
  asked to triage issues, find stale issues, clean up the backlog, identify what
  can be closed, or audit a GitHub repository's open issue list.
license: BSD-3-Clause
metadata:
  category: process
---

# GitHub Issue Triage

Produces a backlog health report: which open issues are done, superseded, or
still real — backed by evidence from the live codebase and git history.

Three deterministic scripts are provided in `scripts/`:

| Script | Purpose |
|--------|---------|
| `fetch-issues.sh` | Download all open issues to a JSON file |
| `explore-issue.sh` | Cross-reference a single issue against the local repo |
| `close-issues.sh` | Bulk-close or bulk-comment on a list of issue numbers |

---

## Overview

The core pattern is **enumerate → batch → fan out → synthesize**:

1. Fetch all open issues with full bodies (`fetch-issues.sh`).
2. Read the list and group issues into 4–6 thematic batches.
3. Dispatch one subagent per batch in parallel; each uses `explore-issue.sh`
   and targeted `rg` / `git log` searches to gather evidence.
4. Synthesize per-batch verdicts into a tiered report.
5. Optionally act on the report with `close-issues.sh`.

Parallel agents are essential — a 60-issue backlog serialized would be
impractically slow; batched parallel execution finishes in one round-trip.

---

## Phase 1 — Fetch issues

```bash
./skills/process/github-issue-triage/scripts/fetch-issues.sh \
  mitodl/ol-infrastructure \
  /tmp/issues_full.json
```

The script writes a JSON array. Extract a compact view for batching:

```bash
jq '[.[] | {number, title, labels: [.labels[].name],
            createdAt: .createdAt[:10], updatedAt: .updatedAt[:10]}]' \
  /tmp/issues_full.json
```

---

## Phase 2 — Batch by theme

Read the compact list and group issues into 4–6 thematic batches. Good split axes:

- **Feature domain** — monitoring, release pipeline, auth/SSO, data platform, etc.
- **Component** — per-service or per-infrastructure layer (edxapp, vault, k8s, etc.)
- **Age** — oldest/most-likely-stale issues as one batch

Target 8–15 issues per batch. Too small = wasted agent overhead; too large = the
agent skims rather than investigates each issue.

Extract bodies per batch for agent prompts:

```bash
# Get full data for a specific set of issue numbers
jq '[.[] | select(.number | IN(101, 102, 103))] |
    [.[] | {number, title, body: .body[:800]}]' \
  /tmp/issues_full.json
```

Truncate bodies to ~800 characters — enough for an agent to understand scope
without flooding its context.

---

## Phase 3 — Dispatch parallel agents

Spawn one agent per batch simultaneously. Each agent brief must include:

- The issue data (number, title, body excerpt, creation date)
- The verdict rubric (see below)
- A concrete set of `rg`, `git log`, and file checks calibrated to that batch

### Exploration utility

For individual issue deep-dives, `explore-issue.sh` automates the standard
battery of checks:

```bash
./skills/process/github-issue-triage/scripts/explore-issue.sh \
  1749 \
  /path/to/local/repo \
  /tmp/issues_full.json
```

Output includes: keywords derived from the title, recent matching commits,
code file references, remote branches, and closed PRs referencing the issue.

### Agent prompt template

```
You are auditing open GitHub issues for <repo> (at <local-path>) to determine
which are outdated or superseded by the current code.

For each issue, determine:
1. Has the work been completed? (look for the expected artifact, named commit,
   or PR merged after the issue was opened)
2. Has the underlying technology changed, making this obsolete?
3. Is it superseded by a newer, more specific issue?

Return LIKELY_OUTDATED, POSSIBLY_OUTDATED, or STILL_RELEVANT for each, with
1–2 sentences of reasoning and supporting evidence (file paths, commit hashes).

Issues:
<paste batch JSON here>

Specific searches to run:
<list targeted rg / git log / ls commands relevant to this batch>
```

### Calibrating per-batch searches

Include specific commands in the agent brief rather than letting the agent
free-form search. Examples, adapted per batch:

```bash
# Did the expected artifact get created?
ls /repo/src/<expected-path>/ 2>/dev/null
rg -r "<keyword>" /repo/src --include="*.py" -l

# Any commits related to this issue since it was opened?
git -C /repo log --oneline --since="<issue-created-date>" \
  --grep="<keyword>" --regexp-ignore-case | head -15

# Is a referenced line still in its original state?
grep -n "<specific-string>" /repo/<file> | head -5

# Was the old technology replaced?
rg -r "<old-tool>" /repo/src -l | head  # expect empty
rg -r "<new-tool>" /repo/src -l | head  # expect populated

# Is there an active feature branch?
git -C /repo branch -r | grep -i "<keyword>" | head
```

---

## Phase 4 — Synthesize the report

Collect all agent outputs and build the final report in tiers:

### Tier 1: Close — work clearly done or superseded
Direct evidence: the artifact exists in code, a named commit landed the fix, or
a newer issue explicitly covers the same scope.

### Tier 2: Close — superseded by a newer issue
Older epics or exploratory issues where 2+ newer, concrete issues now track the
same work. Reference the superseding issue numbers.

### Tier 3: Verify then close
Probably done but no smoking-gun commit — work may have landed in a sibling repo,
or the problem was resolved indirectly. Flag for a 5-minute manual check.

### Tier 4: Keep — quick wins
Confirmed gap that could be addressed in a small PR (a one-line config change, a
missing lifecycle policy, a version pin upgrade).

### Tier 5: Keep — active work in flight
A feature branch or recent commits indicate work is underway but not merged.

### Tier 6: Keep — genuine open gap
Agent confirmed the work has not been done and the need remains valid.

---

## Verdict rubric

| Verdict | Evidence threshold |
|---------|-------------------|
| **LIKELY_OUTDATED** | Direct artifact in code, named commit, or newer issue explicitly supersedes |
| **POSSIBLY_OUTDATED** | Partial evidence (related work landed, tech approach changed) but not conclusive |
| **STILL_RELEVANT** | No evidence of completion; gap confirmed by absence search |

Lean toward **POSSIBLY_OUTDATED** when uncertain rather than making a confident
call on thin evidence. The synthesizer can escalate or downgrade after reviewing
all batch outputs together.

---

## Phase 5 — Act on the report

After confirming the list of issues to close (Tier 1 and 2 at minimum, Tier 3
after manual verification):

```bash
# Dry run — see what would happen
printf '1749\n822\n407\n' | \
  ./skills/process/github-issue-triage/scripts/close-issues.sh \
  --dry-run mitodl/ol-infrastructure

# Close with a standard triage comment
printf '1749\n822\n407\n' | \
  ./skills/process/github-issue-triage/scripts/close-issues.sh \
  --close mitodl/ol-infrastructure

# Override the closing comment
ISSUE_TRIAGE_REASON="Closed: superseded by #4828 (Grafana Alerting → Pulumi migration)." \
printf '1749\n' | \
  ./skills/process/github-issue-triage/scripts/close-issues.sh \
  --close mitodl/ol-infrastructure
```

For issues with different reasons, run the script once per group with a custom
`ISSUE_TRIAGE_REASON`.

---

## Common patterns

### "Work done in another repo"
Issue asks for something in this repo; implementation landed in a sibling repo.
Evidence: no local code; git log has no related commits. Verdict: POSSIBLY_OUTDATED —
flag for manual check, not automatic close.

### "Technology replaced"
Issue asks for feature X using tool A, but the team adopted tool B which
inherently provides X. Evidence: tool A absent; tool B present and covers the
use case. Verdict: LIKELY_OUTDATED.

### "Superseded by a newer, more specific issue"
Old epic or exploratory issue, now tracked by 2–5 concrete sub-issues. Evidence:
newer issues reference the old one by number, or their titles clearly cover the
old scope. Verdict: LIKELY_OUTDATED for the old issue; note superseding numbers.

### "Feature branch exists, not yet merged"
Active work. Evidence: `git branch -r | grep <keyword>` returns hits; recent
commits on a non-main branch. Verdict: STILL_RELEVANT — do not close.

### "Bot-maintained issue"
Renovate Dependency Dashboard and similar bot-maintained issues are permanent
tracking surfaces, not actionable items. Skip them in the triage — they close
only when the bot is removed.

---

## Output format

Present the final report as markdown tables, one per tier:

```markdown
| # | Title | Evidence |
|---|-------|----------|
| 1749 | Review Pingdom alerts | Superseded by #4828 (Pingdom → Grafana migration) |
```

Keep the Evidence column to one sentence. At the end, include a bottom-line:
"N issues are strong close candidates; M are trivial code changes; the rest are
genuine open work."

See [references/verdict-examples.md](references/verdict-examples.md) for
annotated examples from a real triage run.
