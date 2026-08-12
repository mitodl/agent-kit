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

## Repo modes

The triage process behaves differently depending on how the target GitHub repo
is organised:

| Mode | Description | Example |
|------|-------------|---------|
| **Direct** | Issues live in the same repo as the code they describe | `mitodl/ol-infrastructure` |
| **Tracker** | A catch-all repo holds issues; labels route each issue to the actual product repo | `mitodl/hq` |

Detect the mode before batching. In tracker repos, each issue's product label
is the routing key to the codebase that needs to be searched. Skipping this
step causes every agent to search the wrong repo (the empty tracker), producing
no evidence and unreliable verdicts.

---

## Overview

The core pattern is **enumerate → resolve → batch → fan out → synthesize**:

1. Fetch all open issues with full bodies (`fetch-issues.sh`).
2. **(Tracker repos only)** Build a label→local-path map and annotate each
   issue with the correct codebase path to search.
3. Group issues into 4–6 thematic batches, keeping same-product issues together
   so each agent searches one codebase.
4. Dispatch one subagent per batch in parallel; each uses `explore-issue.sh`
   and targeted `rg` / `git log` searches to gather evidence.
5. Synthesize per-batch verdicts into a tiered report.
6. Optionally act on the report with `close-issues.sh`.

---

## Phase 1 — Fetch issues

```bash
./skills/process/github-issue-triage/scripts/fetch-issues.sh \
  mitodl/ol-infrastructure \
  /tmp/issues_full.json
```

The script writes a JSON array. Extract a compact view for mode detection:

```bash
jq '[.[] | {number, title, labels: [.labels[].name],
            createdAt: .createdAt[:10], updatedAt: .updatedAt[:10]}]' \
  /tmp/issues_full.json
```

---

## Phase 1b — (Tracker repos only) Build the label→path map

Skip this phase for direct repos. Apply it when the issue repo is a tracker
(e.g. `mitodl/hq`) where product labels like `product:mit-learn` or
`product:infrastructure` indicate which codebase to check.

### Step 1: Identify the product label namespace

Inspect the labels present on the fetched issues:

```bash
jq '[.[].labels[]] | unique | sort' /tmp/issues_full.json
```

Look for a consistent prefix pattern such as `product:`, `component:`, or
`team:`. The prefix and the set of values form the namespace.

### Step 2: Write a label map JSON file

Create a JSON object mapping each product label to the absolute path of the
local checkout for that product's codebase:

```json
{
  "product:infrastructure": "/home/you/code/mit/ops/infra/ol-infrastructure",
  "product:mit-learn":      "/home/you/code/mit/apps/mit-learn",
  "product:mitx-online":    "/home/you/code/mit/apps/mitxonline",
  "product:ocw":            "/home/you/code/mit/apps/ocw-studio",
  "product:xpro":           "/home/you/code/mit/apps/xpro"
}
```

Save it as `/tmp/label_map.json`. If a product repo is not checked out locally,
omit it from the map — `explore-issue.sh` will fall back to the tracker repo
path and note that the target codebase was unavailable.

### Step 3: Annotate each issue with its resolved path

```bash
# Quick annotation: add a resolved_path field to each issue
LABEL_MAP=/tmp/label_map.json
jq --slurpfile lm "${LABEL_MAP}" '
  map(. + {
    resolved_path: (
      .labels as $lbls |
      $lm[0] |
      to_entries |
      map(select(.key as $k | $lbls | contains([$k]))) |
      first.value // "UNRESOLVED"
    )
  })
' /tmp/issues_full.json > /tmp/issues_annotated.json
```

Issues with `resolved_path: "UNRESOLVED"` have no product label — treat them as
infrastructure/meta issues scoped to the tracker repo itself, or handle as a
separate batch with manual label assignment.

---

## Phase 2 — Batch by theme

Read the (annotated) issue list and group into 4–6 thematic batches.

**For direct repos**, split by feature domain or component:
- Monitoring / alerting
- Release pipeline / CI
- Auth / SSO
- Infrastructure (Vault, AWS, K8s)
- Misc / old / meta

**For tracker repos**, split by `resolved_path` first (so each agent searches
one codebase), then subdivide large per-product groups by theme if needed.

Target 8–15 issues per batch. Extract bodies for the agent prompt:

```bash
jq '[.[] | select(.number | IN(101, 102, 103))] |
    [.[] | {number, title, body: .body[:800], resolved_path}]' \
  /tmp/issues_annotated.json
```

---

## Phase 3 — Dispatch parallel agents

Spawn one agent per batch simultaneously. Each agent brief must include:

- The issue data (number, title, body excerpt, creation date, resolved_path)
- The verdict rubric (see below)
- A concrete set of `rg`, `git log`, and file checks calibrated to the batch
- **The correct repo path to search** — for tracker repos this is `resolved_path`,
  not the tracker repo itself

### Exploration utility

`explore-issue.sh` accepts an optional label-map argument to automatically
resolve the target codebase from the issue's labels:

```bash
# Direct repo — single codebase
./scripts/explore-issue.sh 1749 /path/to/repo /tmp/issues_full.json

# Tracker repo — derive codebase from product label
./scripts/explore-issue.sh 1749 /path/to/hq /tmp/issues_annotated.json \
  /tmp/label_map.json
```

When `label_map.json` is supplied, `explore-issue.sh` reads the issue's labels,
looks up the first matching path, and runs all searches there instead of in the
tracker repo. If no label matches, it falls back to the provided `repo-path` and
prints a warning.

### Agent prompt template

```
You are auditing open GitHub issues for <tracker-or-direct-repo>.

For each issue, determine:
1. Has the work been completed? (look for the expected artifact, named commit,
   or PR merged after the issue was opened)
2. Has the underlying technology changed, making this obsolete?
3. Is it superseded by a newer, more specific issue?

Return LIKELY_OUTDATED, POSSIBLY_OUTDATED, or STILL_RELEVANT for each, with
1–2 sentences of reasoning and supporting evidence (file paths, commit hashes).

IMPORTANT: Each issue has a resolved_path field. Search THAT path, not the
tracker repo. The tracker repo contains no product code.

Issues:
<paste batch JSON with resolved_path fields here>

Specific searches to run per issue (adapt the path from resolved_path):
<list targeted rg / git log / ls commands>
```

### Calibrating per-batch searches

```bash
# Did the expected artifact get created?
ls <resolved_path>/src/<expected-path>/ 2>/dev/null
rg -l -g "*.py" "<keyword>" <resolved_path>/src

# Any commits related to this issue since it was opened?
git -C <resolved_path> log --oneline --since="<issue-created-date>" \
  --grep="<keyword>" -E --regexp-ignore-case | head -15

# Is a referenced line still in its original state?
grep -n "<specific-string>" <resolved_path>/<file> | head -5

# Was the old technology replaced?
rg -l "<old-tool>" <resolved_path>/src | head  # expect empty
rg -l "<new-tool>" <resolved_path>/src | head  # expect populated

# Is there an active feature branch?
git -C <resolved_path> branch -r | grep -i "<keyword>" | head
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
  ./scripts/close-issues.sh --dry-run mitodl/ol-infrastructure

# Close with a standard triage comment
printf '1749\n822\n407\n' | \
  ./scripts/close-issues.sh --close mitodl/ol-infrastructure

# Override the closing comment for a specific group.
# The assignment must sit on the script side of the pipe — a prefix on the
# `printf` sets it for `printf` only, and the script silently falls back to
# the generic default.
printf '1749\n' | \
  ISSUE_TRIAGE_REASON="Closed: superseded by #4828 (Grafana Alerting → Pulumi migration)." \
  ./scripts/close-issues.sh --close mitodl/ol-infrastructure
```

For tracker repos, the `--repo` argument is the tracker repo (where the issues
live), not the product repo. The closing comment is posted to the tracker issue.

**The closing comment is read by whoever filed the issue.** The script's
default reason is a generic catch-all — prefer a specific one via
`ISSUE_TRIAGE_REASON`, one sentence naming the actual cause and the superseding
issue or commit: `Closed: superseded by #4828 (Pingdom → Grafana migration).`
Ask the user to supply or approve the wording per group before closing; a batch
close is visible to the whole team and hard to walk back.

---

## Common patterns

### "Work done in another repo" (direct repo)
Issue asks for something in this repo; implementation landed in a sibling repo.
Evidence: no local code; git log has no related commits. Verdict: POSSIBLY_OUTDATED —
flag for manual check, not automatic close.

### "Tracker repo issue with no product label"
The issue has no label matching the label map. It may be a meta/process issue
scoped to the tracker itself (infra, hiring, process), or may have been mislabelled.
Do not search the tracker codebase — instead check the issue body for explicit
mentions of a target repo and search there, or mark as POSSIBLY_OUTDATED with a
note requesting a label.

### "Technology replaced"
Issue asks for feature X using tool A, but the team adopted tool B which
inherently provides X. Evidence: tool A absent in the target codebase; tool B
present and covers the use case. Verdict: LIKELY_OUTDATED.

### "Superseded by a newer, more specific issue"
Old epic or exploratory issue, now tracked by 2–5 concrete sub-issues. Evidence:
newer issues reference the old one by number, or their titles clearly cover the
old scope. Verdict: LIKELY_OUTDATED for the old issue; note superseding numbers.

### "Feature branch exists, not yet merged"
Active work. Evidence: `git -C <resolved_path> branch -r | grep <keyword>`
returns hits; recent commits on a non-main branch. Verdict: STILL_RELEVANT — do
not close.

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
