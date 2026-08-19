# witan hooks, workflow UX & progression — implementation spec

Status: **spec** (accepted; implementation-ready for the P1 slice)
Project: `wp-witan-hooks-workflow-ux-progression-improvements-852aaf`
Basis: [`witan-workflow-hooks-elicitation-evaluation.md`](./witan-workflow-hooks-elicitation-evaluation.md)
(discovery findings, item ids A1–C5) and the discovery-validation memory
`ctx-discovery-validation-witan-hooks-ux-elicitation--2e372f`.

All source anchors below are against `origin/main` @ `8af6c62` (the delivered
surface-refinement base, PR #81), where this branch is rooted.

This spec turns the evaluation's *what/why* into concrete *how*: shared modules,
function signatures, return schemas, CLI shapes, and a per-item test plan. The
P1 slice (§2–§4) is specified to implementation depth; P2/P3 (§6) are design
sketches that defer detail to the eval until they are scheduled.

---

## 1. Slices & task mapping

| Slice | Items | Tasks |
|---|---|---|
| **S1 — correctness sweep** | A1, B6, B7 | `tk-fix-context-hook-double-emission-…`, `tk-fix-tmp-session-state-path-…`, `tk-unify-the-two-divergent-ready-task-…` |
| **S2 — continuity value** | B4, B1 | `tk-add-workflow-project-status-…`, **B1 task created this phase** (see §4.2) |
| **S3 — P2 batch** | A2, A3, B2, B3, B5, C1–C4 | truncation / latency / memory-nudge / staleness / CLI transitions / elicitation tasks |
| **S4 — P3 cleanup** | A4, A5, B8, B9, C5 | debug / repo-detection / task-phase / skill-refresh / repo-elicit tasks |

Order: **S1 → S2 → S3 → S4** (eval "Suggested sequencing"). S1 lands one PR;
S2 a second. S2 depends on nothing in S1 but is sequenced after so the trust
fix (double-emission) ships first.

---

## 2. Shared infrastructure

Two leaf modules, both **pure** (no import of `server`/`context`, so no cycle)
— they hold the single source of truth the divergence bugs violated.

### 2.1 `witan/session_state.py` — one temp-path helper (fixes B6)

```python
import os, tempfile
from pathlib import Path

_STATE_FILE_PREFIX = "workflow-session-"

def session_state_dir() -> Path:
    """Temp dir for session-state files. Honors TMPDIR/TEMP/TMP with the
    stdlib fallback+writability chain, matching tempfile everywhere."""
    return Path(tempfile.gettempdir())

def session_state_path(session_id: str) -> Path:
    return session_state_dir() / f"{_STATE_FILE_PREFIX}{session_id}.json"

def iter_session_state_files() -> list[Path]:
    return sorted(session_state_dir().glob(f"{_STATE_FILE_PREFIX}*.json"))
```

Callers rewired to this module:
- `server.py:1231-1235` (`_STATE_FILE_PREFIX`, `_session_state_path`) → re-export/delegate.
- `server.py:2098-2099` cleanup glob → `iter_session_state_files()`.
- `context.py:195-196` (`session_checkpoint`) → `session_state_path(session_id)`.
  This is the actual bug site: it hardcodes `os.environ.get("TMPDIR","/tmp")`,
  which diverges from `tempfile.gettempdir()` when `TMPDIR` is unset but
  `TEMP`/`TMP` is set, or when `/tmp` is non-writable and stdlib falls back.
- `skills/witan-workflow/SKILL.md` glob guidance → describe "your temp dir
  (`TMPDIR`, else the platform default)", not a literal `/tmp`.

### 2.2 `witan/readiness.py` — one ready-task filter (fixes B7)

Lifts the lease logic out of `server.py` (`_CLAIM_LEASE_SECONDS`,
`_lease_expired` at `server.py:195-206`) so both the tool and the hook share it.

```python
from datetime import datetime, timezone

CLAIM_LEASE_SECONDS = 3600
_PRIORITY = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}

def lease_expired(claimed_at: str | None, *, now: datetime | None = None) -> bool: ...

def is_ready(task: dict, status_by_slug: dict[str, str], *, now=None) -> bool:
    """Ready == blockers all closed AND the task is claimable:
      - status in {open, blocked}; OR
      - status == in_progress with an expired lease (reclaimable)."""
    if any(status_by_slug.get(b, "closed") != "closed"
           for b in (task.get("blocked_by") or [])):
        return False
    status = task.get("status")
    if status in ("open", "blocked"):
        return True
    return status == "in_progress" and lease_expired(task.get("claimed_at"), now=now)

def filter_ready(tasks: list[dict], *, now=None) -> list[dict]:
    status_by_slug = {t["slug"]: t.get("status") for t in tasks}
    ready = [t for t in tasks if is_ready(t, status_by_slug, now=now)]
    ready.sort(key=lambda t: _PRIORITY.get(t.get("priority", "p3"), 9))
    return ready
```

Callers rewired:
- `context.py:76-86` (the hook's inline `status in (open, blocked) and all
  blockers closed` + `_PRIORITY` sort) → `readiness.filter_ready(tasks)`. This
  is the divergence: the hook currently omits the `in_progress`+expired-lease
  reclaim path that `task_ready` honors.
- `server.py` `task_ready` (`:2559`, lease check at `:2615`) → `is_ready` /
  `filter_ready`, keeping its own fetch/query but delegating the predicate.

**Behavior change to note in the PR:** the injected "Ready Tasks" list will now
include reclaimable-lease `in_progress` tasks, matching `task_ready`. Intended.

---

## 3. S1 correctness sweep — remaining item

### 3.1 A1 — context block double-emission

Mechanism (confirmed): `adapters/claude.py:merge_hooks` (`:48-72`) dedups on the
**exact** `command` string per event. `witan setup` registers the bare
`witan inject-context` (`setup.py:49`); `docs/agent-memory.md:102,1550,1561-1567`
still tell users to register `bash ~/.claude/hooks/workflow-context-inject.sh`,
whose body runs `witan inject-context` (`hooks/workflow-context-inject.sh:4`).
Different strings → both survive → block prints twice (reproduced live in the
discovery session's prompt context).

Fix (three parts):

1. **Stable hook identity + legacy prune.** Add an optional `identity: str |
   None` to `DeclarativeHook` (default `None` → falls back to `command`, so no
   existing behavior changes). `merge_hooks` dedups on `identity or command`;
   `witan/setup.py` sets `identity="witan:inject-context"` /
   `"witan:session-checkpoint"`. Add a `legacy_commands: list[str]` to the
   witan hooks and have the apply path call `remove_hooks` for them (the
   inverse already exists at `claude.py:75-103`) before `merge_hooks`, pruning
   any pre-existing `.sh`-wrapper entry — a targeted migration, not blanket
   `--prune`.
   Legacy commands to prune: `bash ~/.claude/hooks/workflow-context-inject.sh`,
   `bash ~/.claude/hooks/workflow-session-checkpoint.sh`, and the
   `$REPO/configs/hooks/…` symlink forms.
2. **Docs.** Rewrite `docs/agent-memory.md:102,1550,1561-1567` to register the
   bare `witan inject-context` / `witan session-checkpoint` commands; drop the
   `ln -sf …workflow-context-inject.sh` step.
3. **Collapse source-of-truth dirs.** The `.sh` wrappers exist in both
   `witan/witan/hooks/` and (per docs) `configs/hooks/`. Keep exactly one — the
   packaged `witan/witan/hooks/` — as an optional escape hatch, and stop the
   docs/setup pointing at `configs/hooks/`.

Tests: `tests/test_setup.py` — applying twice yields one entry per event;
applying over a pre-existing legacy `.sh` entry replaces (not appends) it.

---

## 4. S2 continuity value

### 4.1 B4 — `workflow_project_status` tool + `witan project status` CLI

New MCP tool (sync; no elicitation):

```python
@mcp.tool
def workflow_project_status(slug: str) -> dict:
    """One-call resume view: phase + ready tasks + last session + blockers."""
```

Return schema:

```jsonc
{
  "project": {"slug", "title", "phase", "status", "repos", "github_pr"},
  "ready_tasks": [{"slug","title","priority","status","assignee"}],  // via readiness.filter_ready(list_tasks_by_project)
  "last_session": {"slug","summary","ended_at","open": bool} | null,  // latest of list_sessions_by_project, open == ended_at is null
  "blockers": ["wp-…"],          // project.blocked_by
  "counts": {"ready": N, "open_tasks": M}
}
```

Reuses existing read queries only — `get_workflow_project`,
`list_tasks_by_project`, `list_sessions_by_project` (all present in
`queries/read.gq`). "Latest session" = max by `ended_at` else `created_at`.

CLI: add `project status <slug>` to `cli/projects.py` (alongside
`_project_show` at `:74`), rendering the same four sections via the existing
`_render_project` helper (`:233`) plus a ready-task table and the last-session
line. Human-readable default; `--format json` passes the tool payload through
(per the omnigraph-v0.7 CLI convention in memory `feedback_omnigraph_v070_cli`).

### 4.2 B1 — surface last session summary on resume (context hook)

The highest-value continuity gap: `inject_context` (`context.py:27-149`) never
reads sessions, so the written handoff summary is invisible on resume. **No task
existed for this** — created this phase (see task creation below).

Design: after the "Active Workflow Projects" block, for each shown project
(top 3), fetch `list_sessions_by_project`, take the latest, and append:

```
  Last session: <summary first line> (<ended <ts> | still open>)
```

Wrap the session fetch/render in its own `try/except → skip` block, isolated
exactly like the existing CodeBranch block (`context.py:58-73`), so a query
failure never blanks the projects/ready context that already works. Truncate
the summary to one line / ~200 chars. Depends on nothing in B4 but shares the
"latest session" selection rule — factor a tiny `latest_session(sessions)`
helper (in `context.py`, or `readiness.py` if reused by B4's tool).

Tests: `tests/test_context.py` — with a session present, the block contains the
summary line and the open/ended marker; with the session query raising, the
projects/tasks block is still emitted intact.

---

## 5. Cross-cutting constraints

- **Elicitation is additive-only** and out of the P1 slice. When S3 adds it
  (C1–C4), introduce `_elicit_or(ctx, ..., default)` (try `ctx.elicit`, catch
  unsupported/declined → default) and convert only those four tools to
  `async def` with `ctx: Context`. Never gate the headless paths
  (`session_start`/`_end`, `project_list`, `memory_store`, `code_reindex`,
  `workflow_trace_mine`) on it.
- **Hooks must stay fault-tolerant**: every new hook code path keeps the
  `except Exception: return ""`/`pass` envelope; new work goes in isolated
  try-blocks so one failing query can't blank the rest.

---

## 6. P2/P3 design sketches (deferred detail — see eval)

- **A2 honest truncation** (`context.py:97,130,133`): print "showing top 5 of N".
- **A3 latency** (`context.py:65,165-169`): set `timeout_seconds` on the
  declarative hook; add a short-TTL repo/branch cache mirroring witan-code's
  `_cached_git`; consider collapsing reads.
- **B2 no-session memory nudge** (`server.py:846-847`): `memory_store` result
  flags when it stored without an active session; hook nudges when a repo has an
  active project but no session-state file.
- **B3 staleness + soft advance-validation** (`server.py:1431`): hook staleness
  line; `workflow_project_advance` returns a note on backward/skip transitions
  (no hard block).
- **B5 CLI transitions**: add `project advance|complete|block`, `session
  end`, `task close|claim|release|link|update` to the CLI — the transition
  surface is MCP-only today.
- **A4** `inject-context --debug`; **A5** repo detection beyond `origin`;
  **B8** optional task `phase` tag; **B9** skill refresh + `workflow_trace_get`;
  **C5** elicit repo on `None` with headless fallback.

---

## 7. Definition of done (P1 slice)

- `session_state.py` + `readiness.py` exist; `server.py` and `context.py` both
  import them; no inline duplicate of either path/predicate remains.
- `witan setup` applied twice → exactly one hook entry per event; a pre-existing
  legacy `.sh` entry is pruned; `docs/agent-memory.md` no longer registers `.sh`.
- `workflow_project_status` tool + `witan project status` CLI return the four
  sections; injected context shows the last-session summary line.
- `pre-commit`, `mypy`, and the witan test suite pass; new tests per §3.1/§4.
