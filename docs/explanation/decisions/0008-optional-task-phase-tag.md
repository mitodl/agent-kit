<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan/docs/adr/0008-optional-task-phase-tag.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan/docs/adr/0008-optional-task-phase-tag.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/adr/0008-optional-task-phase-tag.md).

# 8. Optional task `phase` field + per-phase ready-work rollup

> "Phase tag" is used loosely in the tracking task title; the decision below is a
> typed, optional `phase` **enum field** on `Task` (not a free-form tag) — see
> the Alternatives section for why a field beats a tag value.

- Status: Proposed (design; implementation deferred)
- Date: 2026-07-08
- Deciders: witan platform owners
- Tracking: task `tk-optional-task-phase-tag-per-phase-task-rollup-de-c95687`, project `wp-witan-hooks-workflow-ux-progression-improvements-852aaf`
- Supersedes: —
- Related: eval `docs/internals/design/witan-workflow-hooks-elicitation-evaluation.md` §B8; `docs/adr/0003-atomic-task-claims-cas.md`

## Context

A `WorkflowProject` moves through phases (`discovery → spec → implementation →
delivery`), and a `WorkflowSession` records the phase it worked in. **Tasks are
phase-agnostic**: `node Task` (`schema/schema.pg:171`) has no `phase` field, and
`insert_task` never sets one. Consequences:

- Advancing a project's phase (`workflow_project_advance`) has **no effect** on
  its tasks — nothing connects "this task belongs to the spec phase" to the
  project being in spec.
- There is **no per-phase ready-work rollup**: `task_ready` / the context hook
  surface every ready task under a project regardless of which phase it belongs
  to, so an agent resuming a project in `implementation` still sees leftover
  `discovery`-phase tasks mixed in with no way to scope to "the current phase's
  work".

This is a real ergonomic gap but a **low-severity, additive** one — nothing is
broken, tasks simply can't be sliced by phase. This ADR designs the smallest
change that closes it and records the schema/query/tool implications so the
implementation is a mechanical follow-up.

### Forces

- **Additive-only.** Existing tasks (thousands, unscoped-by-phase) must keep
  working unchanged; `phase` has to be optional with a null-tolerant default.
- **Consistency with existing enums.** Project/session already use
  `enum(discovery, spec, implementation, delivery)` (`schema.pg:85,107`); a task
  phase should reuse the exact same vocabulary, not invent a parallel one.
- **Readiness must not regress.** `task_ready`'s core contract (open + all
  blockers closed, lease-aware) is shared with the context hook via
  `readiness.filter_ready`. A phase filter must be a *narrowing* applied on top,
  never a change to the readiness rule itself.
- **omnigraph field addition is a schema migration.** Adding a node field means
  `witan migrate schema` must run; reads of old rows must tolerate a missing
  `phase` (treated as `None`).

## Decision

Add an **optional `phase` field on `Task`**, reusing the project/session enum,
and thread it through create/update/read as a *filter*, not a gate.

### 1. Schema (`schema/schema.pg`)

```
node Task {
    ...
    priority:    enum(p0, p1, p2, p3) @index
    phase:       enum(discovery, spec, implementation, delivery)? @index  // NEW — optional
    ...
}
```

Nullable (`?`) and `@index` (phase filters are equality scans, matching how
`status`/`priority` are indexed). No new edge — phase is an attribute of the
task, not a relationship; the task→project link (`TaskBelongsTo`) already
carries project membership, and the project already owns the *current* phase.

### 2. Mutations (`queries/mutations.gq`)

- `insert_task` gains a `phase` param (nullable), written alongside the other
  fields. Old callers that omit it persist `null` — today's behavior.
- A dedicated `update_task_phase` is unnecessary: the generic task update path
  (`_update_task`) already writes arbitrary fields, so `task_update` gains a
  `phase` argument for free.

### 3. Server tools (`server.py`)

- `task_create(..., phase: WorkflowPhase | None = None)` — passes through to
  `insert_task`.
- `task_update(..., phase: WorkflowPhase | None = None)` — re-phase a task.
- `task_ready(..., phase: WorkflowPhase | None = None)` — when given, filter the
  ready set to tasks whose `phase == phase` **after** `readiness.is_ready`
  (narrowing only; the readiness rule is untouched). Tasks with a null phase are
  **excluded** from a phase-scoped query but always included in an unscoped one,
  so the default surface is unchanged.
- Optional convenience: `phase="__current__"` sentinel, or a separate
  `task_ready_for_current_phase(project_slug)` that resolves the project's phase
  and filters to it — the "surface this phase's ready work" one-liner. Recommend
  deferring this until the plain filter proves useful, to avoid API surface we
  might not need.

### 4. Context hook (`context.py`)

`inject_context` already knows each active project's current `phase`
(`p['phase']`). Once tasks carry a phase, the "Ready Tasks" section can *prefer*
the current phase: show current-phase ready tasks first (or exclusively, with a
"+N in other phases" note), while null-phase tasks stay visible so nothing is
hidden from the injected block. This is presentation-only and can ship after the
schema/tool change.

### 5. CLI

- `witan task create --phase …`, `witan task update --phase …`.
- `witan tasks --ready --phase …` (the `tasks` command's existing `--ready`
  path gains a phase filter).
- `witan project tasks --phase …` already filters by `--status`; add `--phase`
  symmetrically (see the `project tasks` subcommand added in this project).

### 6. Migration

- Bump the bundled schema and require `witan migrate schema` (the existing
  path — CodeBranch was added the same way; `context.py` already tolerates a
  store that predates a field). Old tasks read back with `phase = None`.
- No data backfill: a null phase is a valid, intended "unphased" state. Agents
  can set a phase on new tasks going forward; historical tasks stay unphased.

## Consequences

- **Purely additive.** A null `phase` means "unphased", the default for every
  existing task and every caller that omits the argument. No existing query,
  tool, or CLI behavior changes until a caller opts into `phase`.
- **Readiness stays single-sourced.** The phase filter is a post-`is_ready`
  narrowing shared nowhere else, so `task_ready` and the context hook cannot
  diverge on *readiness* (the bug B7 fixed); they only differ in whether they
  choose to narrow by phase.
- **Phase is advisory, not a state machine.** A task's phase does not gate its
  readiness and is not auto-advanced when the project advances — mirroring the
  project's own "phases are flexible, not enforced" stance (ADR-less, but see
  `_advance_advisory`). Coupling task phase to project phase transitions is
  explicitly out of scope.
- **Cost is one migration + a handful of optional params.** No new node or edge
  type, no readiness-rule change, no backfill.

## Alternatives considered

- **A `phase` *tag* in the existing `tags: [String]?` list** (no schema change).
  Rejected: tags are free-form and unindexed for this purpose; a typed enum
  field gives validation, an index, and parity with project/session, and avoids
  overloading `tags` with a semantically special value.
- **A `PhaseContains: WorkflowProject -> Task` edge** instead of a field.
  Rejected: phase is an attribute of the task in the context of its project, not
  an independent relationship; an edge adds traversal cost to every ready-work
  query for no expressive gain, and a task belongs to exactly one phase at a
  time (an attribute, not a many-to-many).
- **Auto-assign a task's phase from the project's phase at creation.** Rejected
  as a default: it silently backdates tasks filed for future phases and couples
  task creation to project state; leave phase explicit and optional.

## Rollout

Design **proposed** here (Status: Proposed — acceptance pending review);
implementation is a follow-up task (schema field +
`insert_task`/`task_create`/`task_update`/`task_ready` params + CLI flags +
migration note + tests for the null-phase-default and phase-narrowing paths).
The context-hook "prefer current phase" presentation is a separate, later slice.
