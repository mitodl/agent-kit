---
name: deploy-verification
description: >
  Verify that a merged infrastructure, config, or manifest change actually
  took effect in the deployed environment — not just that it merged. Use
  this skill when asked to "verify the deploy", "confirm this is live",
  "check the rollout", "did this actually deploy", or after any
  ConfigMap/Secret/env/Pulumi/Helm change where the task isn't done until
  the effect is confirmed in the running system. Covers CD pipeline status,
  pod rollout confirmation, reading config out of the running process,
  before/after metric comparison via Grafana/Prometheus, and confirming a
  change didn't leak into an unintended environment.
license: BSD-3-Clause
metadata:
  category: process
---

# Deploy Verification

A config or manifest change merging is not the same as it taking effect.
Three separate things can each silently fail while the previous one
succeeds: the CD pipeline can be pending or red, the pods can never
actually restart even though the pipeline ran, and the restarted pods can
still be running the old value if the change didn't reach the path you
think it did. This skill walks all three, in order, and demands evidence at
each step rather than inferring success from the step before it.

**Core rule: report pass/fail per step with the raw evidence** (a pod age,
a rollout status, a query result, a preview diff) — "looks deployed" or
"should be live now" is not a verification, it's a guess with better
formatting. If a step can't be checked (no cluster access, no query result
yet), say so explicitly rather than skipping it silently.

## Step 1 — Confirm the CD pipeline ran and succeeded

Find the deploy workflow for the merged PR's repo and confirm a run
completed **after** the merge commit landed, not just that one exists:

```bash
gh run list -R <owner>/<repo> --workflow <deploy-workflow>.yml --limit 5 \
  --json databaseId,status,conclusion,headSha,createdAt
```

Match `headSha` (or the commit it built from) against the actual merge SHA
— a run against a stale SHA doesn't cover this change. `status: in_progress`
means wait and re-check, not "probably fine." A `conclusion: failure` stops
here: the change never reached the cluster, and nothing past this step is
worth checking yet.

## Step 2 — Confirm the pods actually rolled

A green pipeline only means the deploy step ran, not that the workload
picked it up. Check pod age against the deploy time:

```bash
kubectl get pods -n <namespace> -l app=<app> -o wide
kubectl rollout status deployment/<name> -n <namespace>
```

If pod `AGE` predates the pipeline run, the workload did not restart. This
is the single most common silent failure in this checklist: a
`ConfigMap`/`Secret` change with no checksum/annotation-based restart
trigger on the pod template is inert until something else causes a restart.
Check whether the Pulumi/Helm resource actually wires a config-hash
annotation (or equivalent) into the pod spec — if it doesn't, the merge
changed the manifest but nothing forced a rollout.

Don't run `kubectl rollout restart` yourself to force it — that's a
visible, mutating action on a live workload. Report that pods haven't
rolled and ask before restarting anything, unless the user has already
asked you to force the rollout as part of this task.

## Step 3 — Read the config out of the running process

Confirm the *value*, not just that the pod is new. `kubectl get configmap
-o yaml` only shows what Kubernetes has stored, not what the running
process loaded — the two can differ if the app caches config at startup or
reads from a mounted file with its own reload semantics.

```bash
kubectl exec -n <namespace> <pod> -- env | grep <VAR>
# or, for a file-mounted config:
kubectl exec -n <namespace> <pod> -- cat <mounted-path>
# or, if the app exposes one, a debug/health endpoint that echoes live config
```

If the app has no way to introspect its running config, say that's the
limitation rather than assuming the new pod means the new value is loaded.

## Step 4 — Query the metric the change was supposed to move

Use whichever `toolhive-swe` tier matches the environment
(`toolhive-swe-ci`, `toolhive-swe-qa`, `toolhive-swe-prod` — see this
repo's `agent-config.toml`) to query Prometheus directly rather than eyeballing
a dashboard:

```
mcp__toolhive-swe-<tier>__grafana_query_prometheus
```

Compare a post-deploy window against a prior baseline of **at least the
same length** — a 2-hour post-deploy window against a 2-hour pre-deploy
window is a fair comparison; a 2-hour post-deploy window against "it looked
fine on the dashboard" is not. For anything with daily/weekly seasonality
(request volume, error rate), compare against the same time-of-day/day-of-week
a week prior, not just the hours immediately before the deploy. State the
exact query and the window in the report — a reader should be able to rerun
it.

If the metric hasn't moved the expected direction, or hasn't moved at all,
say that plainly. Don't extend the window, change the query, or reach for a
different metric to manufacture a positive result — report the mismatch and
let the user decide whether it's a slow rollout, a wrong hypothesis, or a
genuine regression.

## Step 5 — Confirm the change didn't leak into an unintended environment

Infra changes are usually per-stack (CI/QA/Prod); a change meant for one
stack landing in another is its own bug. Diff every stack that shares the
changed code, not just the target one:

```bash
pulumi preview --stack <other-stack>
```

`"0 changes"` on every stack except the intended target confirms scope. Any
unexpected diff on an unrelated stack is a blocker, not a footnote — surface
it before calling the deploy verified.

## When to stop and ask instead of deciding

- **Pods haven't rolled and the reason isn't obvious** — don't force a
  restart unprompted (Step 2); report and ask.
- **The pipeline is still running** — wait or say so; don't infer success
  from a merge alone.
- **The metric contradicts the expected effect** — report it as a mismatch,
  not as "probably needs more time," unless you have evidence (e.g. traffic
  volume) that supports that read.
- **An unintended-stack diff shows up in Step 5** — this is a scope bug in
  the original change, not something to quietly ignore because the intended
  stack looks correct.

## Environment

Requires `kubectl` configured with a context for the target cluster, the
`pulumi` CLI with access to the relevant stacks, `gh` for CD pipeline status,
and at least one `toolhive-swe-{ci,qa,prod}` MCP server registered (see
`agent-config.toml`) for the Grafana/Prometheus query step. Skip a step
outright (and say so) rather than guessing when the required access isn't
available.
