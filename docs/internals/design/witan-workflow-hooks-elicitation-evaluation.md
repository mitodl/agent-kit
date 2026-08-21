# witan hooks, workflow UX & elicitation — evaluation

Status: evaluation (findings + prioritized recommendations)
Scope: the witan agent-harness hooks, the workflow project/session/task
progression model, and where MCP elicitation should be introduced.
Method: source read of `mcp/servers/witan` + `packages/agent-config-kit`; file:line
references throughout. No behavior changed by this document.

## TL;DR

The tracking substrate (graph nodes, edges, tools) is solid. The gaps are in
**surfacing** and **correctness of the glue**, not the data model:

1. The context hook emits its block **twice** per prompt (config drift between
   the old docs and current `witan setup`) — visible, wasteful, erodes trust.
2. The single most valuable continuity artifact — the session-end handoff
   summary — is **written but never surfaced on resume**. A new session sees the
   project title and phase but not "where things stand."
3. Two silent-failure bugs: a `/tmp` path computed two different ways (sessions
   can leak open forever) and two divergent "ready task" definitions (the
   injected list disagrees with `task_ready`).
4. Progression is documentation-only — no nudge to advance/complete, no
   "what next" view, no human CLI path to drive transitions.
5. Elicitation is available (FastMCP 3.4.3) and unused; it fits a handful of
   confirmation/ambiguity points, but must be additive-only because several
   tools run headless in hooks.

Priority matrix at the end.

---

## A. Hooks

witan ships exactly two hooks (`setup.py:34-68`): a `UserPromptSubmit`
context-injection hook (`witan inject-context` → `context.py:27-149`) and a
`Stop` session-checkpoint hook (`witan session-checkpoint` → `context.py:185-236`).
Only `claude` (JSON merge) and `pi` (`.ts` extension) receive them; copilot/
opencode/kilo get none (`registry.py:27-164`).

### A1 — Context block emitted twice per prompt · **P1 / bug**

Root cause: config drift with a dedup that keys on the exact command string.
- Current `witan setup` registers the bare command `witan inject-context`
  (`setup.py:49`).
- The still-current docs (`docs/agent-memory.md:1556-1571`) tell users to
  register `bash ~/.claude/hooks/workflow-context-inject.sh`, and that script
  itself runs `witan inject-context` (`hooks/workflow-context-inject.sh:4`).
- Claude's hook dedup appends unless an entry with a byte-identical `command`
  already exists (`adapters/claude.py:62-72`). The two strings differ, so **both
  survive and both fire** — the block prints twice (exactly what this session
  has been seeing). `setup_cmd.py:105-114` keeps installing the `.sh` wrappers,
  keeping the legacy path live.

Recommend:
- Pick one canonical command and make dedup robust: match on the
  `witan inject-context` / `witan session-checkpoint` substring (or a stable
  hook id/marker) rather than the whole string, so the wrapper and bare forms
  collapse to one entry.
- Have `witan setup` prune a pre-existing legacy `…workflow-context-inject.sh`
  entry when it writes the bare-command entry (a targeted migration, not just
  `apply --prune`).
- Update `docs/agent-memory.md:1556-1571` to the current form and stop shipping
  the redundant `.sh` wrappers (or make the JSON reference them), collapsing the
  two sources of truth (`witan/witan/hooks/` vs `configs/hooks/`).

### A2 — Silent truncation with a misleading count · **P2**

Projects are capped at 3 (`context.py:97`), ready tasks at 5 (`context.py:133`),
git diff at 50 files (`context.py:214`), but the header prints the *full* count:
"42 task(s) are ready" while listing 5 (`context.py:130`). Add an explicit
"showing top 5 of 42 (run `witan tasks` for all)" and the same for projects, so
truncation is honest and points to the full view.

### A3 — Prompt-critical-path latency, no Claude-side timeout · **P2**

The context hook runs on **every prompt** and does 2 git subprocesses
(`git remote get-url origin` `context.py:165-169`; `git branch`
`context.py:65`) plus 3–4 omnigraph reads, all synchronous. Pi caps it at 5s
(`workflow-context.ts:27`); the Claude `DeclarativeHook` sets no
`timeout_seconds` (`setup.py:49`) and the `.sh` wrapper has no `timeout`.
Recommend: set a `timeout_seconds` on the declarative hook; cache repo/branch
detection with a short TTL (witan-code already does this — `server.py:_cached_git`
TTL 2s); consider collapsing the reads into one query.

### A4 — Errors vanish with zero diagnosability · **P3**

Both hooks wrap everything in `except Exception: return ""` (`context.py:55,233`)
and the wrappers add `2>/dev/null || true`. A broken graph, missing `git`
(`FileNotFoundError` is not caught specifically at `context.py:170`, only
`CalledProcessError`), or a misconfigured repo produces a silently blank block —
indistinguishable from "nothing to show." Add a `witan inject-context --debug`
that bypasses the swallow and prints the reason to stderr.

### A5 — Repo detection only via `origin` · **P3**

`_detect_repo` (`context.py:152-179`) requires a remote literally named `origin`.
Repos with a differently-named remote or none get no project/task context.
Consider falling back to the first remote, or `WITAN_REPO`, and documenting the
requirement.

---

## B. Workflow progression & UX

### B1 — Session handoff summaries are never surfaced on resume · **P1 / highest-value**

`workflow_session_end` exists precisely to capture "what was done / what remains /
blockers" (`server.py:2082-2085`; skill `witan-project-tracker/SKILL.md:115`),
but the context hook (`context.py:27-149`) queries projects, repo tasks, and
branch tasks — **never** `list_sessions_by_project`. So the one artifact written
for continuity is invisible when a new session resumes; the agent only re-learns
state by proactively calling `workflow_project_memories` or a human running
`witan project <slug>`. Fix: the context block should include, per active
project, the **latest session's summary** (and whether it's still open) — the
"where things stand" line the whole handoff mechanism is designed around.

### B2 — Silent provenance loss when `workflow_session_start` is skipped · **P2**

Memories stored while no session is registered are not linked to the project
(`SessionProduced` skipped, `server.py:846-847`) — absent from
`workflow_project_memories` session-grain and from the completion trace, with no
warning. Since `session_start` is easy to forget, this loses real work silently.
Fix options: (a) surface a one-line nudge in the context hook when an active
project exists for the repo but no session state file is present; (b) have
`memory_store` note in its result when it stored without an active session so the
agent can react; (c) longer-term, auto-open a lightweight session.

### B3 — No progression validation, no advance/complete nudge · **P2**

`workflow_project_advance` (`server.py:1431-1459`) is a bare write of any phase —
backward, skip-ahead, and complete-from-`discovery` are all silently allowed
(intended flexibility, but indistinguishable from mistakes). Nothing ever signals
a project has sat in one phase for N sessions/days. Recommend: (a) a staleness
line in the context hook ("in `implementation` for 6 sessions — advance or
complete?"); (b) soft validation on advance — a note in the result on a
backward/skip transition, not a hard block.

### B4 — No "what should I do next" view · **P1**

The closest primitives are `task_ready(project_slug=…)` and CLI `project show`
(`cli/projects.py:74-135`, human-only, static). Nothing combines
**phase + ready tasks + last-session summary + blockers** into one answer. Add a
`workflow_project_status(slug)` tool returning exactly that — the single-call
resume view an agent (and B1's hook) can lean on — plus a `witan project status`
CLI.

### B5 — CLI can inspect and create, but not transition · **P2**

The CLI (`cli/`) has `projects`/`project show|create|run`, `tasks`/`task
show|create|run`, `traces`/`trace show|list`. There is **no** `project
advance|complete|block`, **no** `session` command at all, **no** `task
close|claim|release|link|update`. Every state *transition* is MCP-only; a human
must launch an agent (`project run`/`task run`) or call MCP tools. For a system
whose pitch is human-inspectable progression, the human side is inspect-only.
Add the transition commands.

### B6 — `/tmp` session-state path computed two ways · **P1 / bug**

`workflow_session_start` and `_active_session_slug` use
`tempfile.gettempdir()` (`server.py:1261`); the Stop hook hardcodes
`os.environ.get("TMPDIR","/tmp")` (`context.py:196`); the `/witan-workflow` skill
globs `/tmp/workflow-session-*.json` (`SKILL.md:113-117`). Under a non-default
`TMPDIR` these diverge, the auto-close silently no-ops, `ended_at` stays null, the
session leaks open forever and is dropped from the trace's duration. Fix: one
shared path helper used by server, hook, and skill guidance.

### B7 — Two divergent "ready" definitions · **P1 / bug**

`task_ready` honors lease expiry and the `in_progress`-reclaim path
(`server.py:2686-2695`); the context hook reimplements readiness
(`context.py:76-86`) and does **not**. The injected "Ready Tasks" list and
`task_ready()` can disagree, which is confusing when an agent acts on the injected
list. Extract one shared readiness helper and call it from both.

### B8 — Tasks are phase-agnostic · **P3 / design**

`insert_task` has no phase field (`server.py:2270-2289`); advancing a project's
phase has no effect on its tasks, and there's no per-phase task rollup. Consider
an optional `phase` tag on tasks so a phase can show its own ready work.

### B9 — Skills are partly stale/thin · **P3**

`witan-project-tracker` tells the agent to hand-construct the trace slug
(`wt-{slug}`) because no `workflow_trace_get` MCP tool exists, and overstates what
the trace node stores (says tools/files; the node holds only
session_count/phases/duration/outcome — `server.py:1528-1547`). `witan-workflow`
hardcodes the `/tmp` glob (see B6). Refresh both; consider adding
`workflow_trace_get`.

---

## C. Elicitation

FastMCP 3.4.3 on mcp 1.28.1 (`witan/uv.lock:358,644`) supports `ctx.elicit`.
**Neither server uses `Context` today** — adding elicitation to a tool means
converting it to `async def` with a `ctx: Context` param. `ctx.elicit` fails/hangs
without client support, so every use needs a non-interactive fallback (try/except
→ today's behavior).

### Must stay non-interactive (headless hook/automation paths)
`workflow_session_start`/`_end` (Stop hook auto-close), `workflow_project_list`
(context hook), `memory_store` (automation), `code_reindex` (Pi background
indexer), and `workflow_trace_mine` (its two-call handshake IS the
non-interactive contract). Do not gate these on elicitation.

### Good fits (agent-in-the-loop, on-demand) — add with fallback
- **C1 · `task_claim` force-steal** (`server.py:2535,2593`): replace the
  all-or-nothing `force` with a confirm — "held by {who} since {when}; steal?".
- **C2 · `memory_link(kind="supersedes")`** (`server.py:1085`): confirm before
  hiding the older memory from default search. Other link kinds need no prompt.
- **C3 · `workflow_project_complete`** (`server.py:1462`): confirm the
  irreversible sealing; if `outcome` is thin, elicit a real narrative before
  minting the immutable trace.
- **C4 · `workflow_project_advance`** (`server.py:1431`): elicit a one-line "why
  advance" note (currently no rationale is captured at a transition at all).

### Handle carefully (on hook-driven paths)
- **C5 · repo=None on `memory_store`/`task_create`** (`server.py:812,2257`):
  when `detect()` returns None and no `repo` was passed, a repo-less node is
  silently persisted — `task_update`'s own docstring admits to retroactively
  fixing these. Eliciting the repo is the right interactive behavior, but these
  paths also run under automation, so it must fall back to today's silent-null
  when non-interactive.

Recommendation: elicitation is worthwhile but **secondary** to A1/B1/B4/B6/B7.
Introduce a small `_elicit_or(ctx, ...)` helper (try elicit, catch
unsupported/declined → default) and apply it to C1–C4 first; treat C5 as a
follow-up given its automation exposure.

---

## Priority matrix

| # | Item | Kind | Priority | Effort |
|---|------|------|----------|--------|
| A1 | Context block double-emission (config drift + string dedup) | bug/UX | **P1** | S |
| B6 | `/tmp` session-state path divergence → leaked-open sessions | bug | **P1** | S |
| B7 | Divergent "ready" definitions (hook vs task_ready) | bug | **P1** | S |
| B1 | Surface last session summary on resume | UX/effectiveness | **P1** | M |
| B4 | `workflow_project_status` "what next" view (+ CLI) | UX/effectiveness | **P1** | M |
| A2 | Honest truncation counts ("top 5 of 42") | UX | P2 | S |
| A3 | Hook latency: timeout + cache repo/branch detect | efficiency | P2 | M |
| B2 | Nudge/flag when storing memory with no active session | correctness | P2 | S |
| B3 | Staleness nudge + soft advance validation | UX | P2 | M |
| B5 | CLI transition commands (advance/complete/close/session) | UX | P2 | M |
| C1–C4 | Elicitation on claim-steal / supersede / complete / advance | UX/safety | P2 | M |
| A4 | `inject-context --debug` diagnosability | ops | P3 | S |
| A5 | Repo detection beyond `origin` | robustness | P3 | S |
| B8 | Optional task phase tag | design | P3 | M |
| B9 | Skill refresh + `workflow_trace_get` | docs | P3 | S |
| C5 | Elicit repo on None (with headless fallback) | correctness | P3 | M |

## Suggested sequencing

1. **Correctness sweep (P1 bugs):** A1, B6, B7 — small, high-trust-impact.
2. **Continuity (P1 value):** B4 (`workflow_project_status`) then B1 (wire its
   summary into the context hook). These compound: B4 builds the view, B1 surfaces it.
3. **P2 batch:** A2/A3 (hook polish), B2/B3 (nudges), B5 (CLI), C1–C4 (elicitation).
4. **P3 cleanup:** A4/A5, B8, B9, C5.

If pursued, this is a natural new tracked project (discovery → spec →
implementation), sized ~4 phases with the P1 items as the first implementation
slice.
