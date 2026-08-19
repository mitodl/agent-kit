# witan & witan-code MCP tool/skill surface refinement — spec

Status: accepted (spec phase)
Project: `wp-witan-witan-code-mcp-tool-skill-surface-refineme-b77803`
Source review: memory `ctx-critical-review-witan-witan-code-mcp-tool-skill--d7c0f0`

## Goal

Reduce agent confusion and inefficient tool selection across the two MCP
servers (`witan`, 37 tools — `mcp/servers/witan/witan/server.py`; `witan-code`,
13 tools — `mcp/servers/witan-code/witan_code/server.py`) and the four skills
(`mcp/servers/witan/witan/skills/witan-{memory,task,workflow,project-tracker}`).

## Posture (decided)

**Breaking rename/remove is in scope.** Tool names, output field names, and
redundant tools may change. There is no external published contract to preserve;
the only consumers are the four in-repo skills and agent muscle memory. Every
rename/removal below is paired with the skill edit that must land in the same
change. No compatibility aliases are added (consistent with repo convention:
change the code, don't shim it).

## Canonical conventions (single source of truth)

These are decided here once and referenced by every task below. Where a
convention is currently restated per-tool, the per-tool prose is deleted and the
convention is hoisted into the server `instructions=` string.

### C1 — Symbol-id format (one spelling, everywhere)

```
<repo_uri>#<relative/path/to/file.py>::<QualifiedName>
```

Concretely: `https://github.com/mitodl/ol-django#app/svc.py::Service.run`.
The path segment **includes the filename**; `QualifiedName` is dotted for nested
symbols (`Outer.method`); a whole-module symbol is `…::<module>`. This is exactly
what the indexer emits (`indexer.py:348` `file_id = f"{slug}#{rel}"`,
`indexer.py:660` `id = f"{file_id}::{qn}"`).

The three current spellings — `repo#path::QualifiedName`,
`repo#path::Qualified.Name`, `repo#path/file.py::Qualified.Name` — are collapsed
to C1. The two abbreviated ones (dropping the filename from the illustrative
path) are wrong and are replaced verbatim.

### C2 — Output field name matches the input param

`witan-code` read tools take a param named `symbol_id`, but the queries project
`$s.slug`, so the round-trip value arrives back under a different key. The
projected identifier field is renamed `slug` → `symbol_id` in the tool output for
Symbol rows. (`CodeFile` rows keep `slug`; they are not symbol ids.) After this,
the value you read from `code_find_definition(...)[i]["symbol_id"]` is exactly
what you pass to `code_find_references(symbol_id=…)`.

### C3 — `repo` parameter (hoisted, stated once)

The 4-line `repo` block repeated on ~20 tools is deleted from each docstring and
stated once in the server instructions:

> Every tool auto-detects the current repo from `.git/config`. Pass `repo` (a
> canonical URI like `https://github.com/mitodl/ol-django`) only to override it.
> Pass `repo=""` to operate across **all** repos.

Per-tool docstrings keep a `repo` line only when the tool's behaviour genuinely
deviates (e.g. the slim-record tri-state in list tools — see C4).

### C4 — Tri-state `repo` in list tools (stated once, referenced)

`memory_list` (and, until removed, `memory_get_project_facts`) has three modes:
detected/explicit repo → scoped full records; `repo=""` → all repos, full
records; no repo detected and none passed → **slim** records (slug, kind, title,
tags; no content) for unscoped memories only. This explanation lives once in the
instructions under "Repo scoping"; list tools reference it in one line rather
than re-explaining it (currently explained verbosely 3×).

### C5 — Prefix convention

Every tool is prefixed by its primary entity: `memory_*`, `task_*`,
`workflow_*`, `topic_*`, `code_*`. **Exactly two** cross-cutting composition
tools are exempt and documented as such: `recall` (composes all memory reads)
and `symbol_context` (spans memory + task by a code symbol). Any other unprefixed
tool is renamed (see Task 5).

### C6 — Docstring content policy

Docstrings describe **what the tool does and when to call it**, for an agent
choosing among tools. They must NOT contain: spec section references
(`spec §7.2`, `§8`), internal doc/schema paths
(`docs/EDGE_PRECISION_TIERS.md`, `SYMBOL_TABLE.md`, `BRANCH_INDEXING.md`,
`schema.pg § …`), changelog/behavior-preservation prose ("preserves prior
behavior", "every binding this tool has always returned"), internal env-var
names (`WITAN_RANK_DEFAULT_CONF`), or implementation apologetics (the task_claim
CAS race explanation). Rationale that belongs to maintainers moves to code
comments; the min_precision explanation moves to instructions (C7).

### C7 — `min_precision` (stated once)

The ~identical 8-line `min_precision` block on
`code_interface_providers/consumers/cross_repo_impact/precise_edges` collapses to
one instructions paragraph plus a one-line per-tool reference:

> `min_precision` (`heuristic` default | `precise`): `precise` keeps only edges
> also confirmed by a canonical-symbol join. Use it to suppress false positives.

### C8 — Error/return convention

One convention across sibling tools:
- **Lookup by slug/id that finds nothing** → return `None` (or a shaped-empty
  result for aggregate returns). Never raise. Aligns `memory_get`/`task_get`
  (already `None`) with `mine_trace`/`workflow_trace_annotate` (currently raise
  on missing trace) and `code_*` (already shaped-empty).
- **Invalid-but-well-formed mutation** (self-link, self-block, claim contention)
  → return a status object `{ "<verb>": false, "reason": … }`. Never raise.
  Aligns `workflow_project_block` self-block (currently raises `ValueError`) with
  `memory_link` self-link and `task_claim` contention (already status objects).
- **Malformed input** (missing required arg, wrong enum) → raise, as today.

## Per-task design decisions

### Task 1 (p1) — Promote `recall` as the primary memory read

`tk-promote-recall-as-the-primary-memory-read-in-ser-d9ae26`

- **Server instructions**: replace "load context with
  `memory_get_project_facts` and `memory_list_patterns`" with a lead on
  `recall`:
  > To load context, call `recall(query=…)` (optionally seed with `symbol_id`,
  > `task`, or `topic`) — it composes every memory read: BM25 + graph expansion +
  > superseded-pruning + re-ranking, and degrades to a plain search when the
  > graph has no edges. Use the narrower `memory_*` reads only for the specific
  > cases in the decision table (skill).
- **witan-memory skill**: add a `recall` section at the top of "When to Use Each
  Tool", ahead of `memory_search`. Show the multi-seed form.
- Cross-referenced by Task 6's decision table (recall = default; narrower tools =
  named exceptions).

### Task 2 (p1) — Fix stale witan-memory skill

`tk-fix-stale-witan-memory-skill-remove-no-supersede-56ee42`

- **Delete** SKILL.md:173-178 ("There is no in-place update or `Supersedes` tool
  yet …"). Replace "Updating an Existing Memory" with the real workflow:
  > Store the corrected memory, then
  > `memory_link(from_slug=<new>, to_slug=<old>, kind="supersedes")`. The old
  > memory is hidden from default `memory_search`/`recall` results but preserved
  > and reachable with `include_superseded=True`.
- **Add** missing tool coverage to the skill: `recall`, `memory_link` (all six
  kinds), `memory_neighbors`, `topic_get`, `memory_for_contract`, tags→topics
  dual-write, and `confidence`. One short subsection each.
- Fix the symbol-id example in "Linking Memories to Code Symbols" to C1 and to
  the renamed round-trip: the id comes back in the `symbol_id` field (C2), not
  `slug`; the reverse lookup is `symbol_context` (renamed, Task 5).

### Task 3 (p2) — Strip internal/changelog/spec prose; hoist shared conventions

`tk-strip-internal-changelog-spec-prose-from-docstri-bd047d`

Apply C3, C6, C7 mechanically across both servers:
- Delete the repeated `repo` block (C3) from every docstring that just restates
  the default; hoist to instructions.
- Delete spec/section refs, internal paths, env-var names, changelog prose (C6).
- Collapse the 4× `min_precision` block to C7.
- Move the `task_claim` CAS "ADVISORY ONLY / read-check-write" paragraph from the
  docstring to an inline code comment; the docstring keeps only "Claim a ready
  task before working it so parallel agents don't collide. Returns
  `{claimed: …}`."
- Net effect target: witan/server.py docstrings shrink materially; no behaviour
  change. Instructions string grows to absorb C3/C4/C7.

### Task 4 (p2) — Reconcile witan-code output `slug` vs input `symbol_id`; unify id format

`tk-reconcile-witan-code-output-field-slug-vs-input--084c53`

- Apply **C2**: in `code_read.gq`, alias the Symbol identifier projection
  `$s.slug` → `symbol_id` for every query returning Symbol rows
  (`find_by_name`, `find_by_qualified_name`, `get_symbol`, `referencers`,
  `callers`, and the `$caller.slug`/`$src.slug` projections at lines 96/113).
  `code_impact`/`code_cross_repo_impact` build their frontier from this field —
  update the Python that reads `caller["slug"]` → `caller["symbol_id"]`
  (`server.py:349`, `:356`, and the `seen`/dedup sets in
  `code_find_references`). `symbols_in_file` and `CodeFile` projections keep
  `slug`.
- Apply **C1**: rewrite the illustrative id string in every docstring/skill to
  the canonical spelling.
- Verification: `code_find_definition(name)[0]["symbol_id"]` fed straight into
  `code_find_references(symbol_id=…)` must resolve. Add/adjust a test asserting
  the field name and the round-trip.

### Task 5 (p2) — Directional renames + prefix consistency

`tk-rename-directional-symbol-memory-tools-and-apply-0fff17`

Rename map (old → new), applied in `@mcp.tool` function names, the four skills,
and any internal callers (`recall` calls `_context_for_symbol`, which stays a
private helper):

| old | new | why |
|-----|-----|-----|
| `context_for_symbol` | `symbol_context` | symbol → {memories, tasks}. Cross-cutting; C5-exempt, documented alongside `recall`. |
| `memory_symbol_context` | `memory_symbols` | memory → its symbols. Reads as "the symbols of this memory"; the reverse is `symbol_context`. |
| `project_memories` | `workflow_project_memories` | project-scoped; restore the `workflow_` prefix (C5). |
| `mine_trace` | `workflow_trace_mine` | trace-scoped; restore the `workflow_` prefix (C5). |

Each renamed tool's docstring names its inverse explicitly ("the reverse of X")
so the pair is discoverable. No behaviour change. The two directional symbol
tools additionally get a one-line "Direction:" note.

### Task 6 (p2) — Consolidate/document the 11 overlapping memory read paths

`tk-consolidate-document-the-11-overlapping-memory-r-3268aa`

Two moves:

1. **Remove the thin wrappers.** `memory_get_project_facts` and
   `memory_list_patterns` are kind-filters over `memory_list`.
   - `memory_get_project_facts()` ≡ `memory_list(kind="project_fact")`.
   - `memory_list_patterns(language=…)` ≡ `memory_list(kind="pattern")` **plus**
     a `language` filter. Add an optional `language: str | None` param to
     `memory_list` (post-filter, mirroring the current pattern) so no capability
     is lost, then delete both wrappers. Update the four skills to call
     `memory_list(kind=…)`.
   - Net: 11 read paths → 9, and the two removed were the most redundant.

2. **Add a decision table** to the instructions and the witan-memory skill.
   Default is `recall`; the table names the *specific* reason to reach for a
   narrower tool:

   | Want | Call |
   |------|------|
   | Contextual load / "what do we know about X" | `recall` (default) |
   | One memory by slug | `memory_get` |
   | Browse all of one kind (no query) | `memory_list(kind=…)` |
   | Plain BM25, no graph expansion | `memory_search` |
   | Neighbours of a known memory, by edge kind | `memory_neighbors` |
   | Everything tagged to a topic (cross-repo) | `topic_get` |
   | Memories + code for a contract key | `memory_for_contract` |
   | Memories/tasks for a code symbol | `symbol_context` |
   | Symbols a memory concerns | `memory_symbols` |
   | Memories a project produced | `workflow_project_memories` |

### Task 7 (p3) — Clarify tri-state `repo=""` / slim-record behavior

`tk-clarify-tri-state-repo-slim-record-behavior-in-m-7de3e0`

Apply **C4**: state the tri-state once under "Repo scoping" in instructions;
reduce the per-tool `repo` docstrings on the list tools to a one-line pointer.
Superseded in part by Task 6 (one of the two tri-state tools,
`memory_get_project_facts`, is removed) — sequence Task 7 after Task 6 so it only
documents `memory_list`.

### Task 8 (p3) — Make witan-code branch-awareness consistent and document it

`tk-make-witan-code-branch-awareness-consistent-and--59f8c1`

Finding: `code_find_definition`/`code_search_symbol` accept `branch` and default
to the checkout branch; `code_find_references`/`code_callers`/`code_impact` route
purely by `symbol_id` and silently read whatever store the id resolves to.

Decision: **make branch-awareness explicit and uniform.**
- The reference/caller/impact tools route by `symbol_id`, whose `repo#path`
  prefix already pins the store. Document that clearly: "Reads the store the
  `symbol_id` belongs to; branch is implied by the id's origin, not a separate
  param." — i.e. accept that these are id-routed and say so, rather than adding a
  `branch` param that would conflict with the id's own repo pin.
- Add a single "Branch semantics" paragraph to instructions covering both
  classes (name-routed tools take `branch`; id-routed tools inherit the id's
  store). No signature change unless implementation finds a name-routed tool
  missing `branch` (audit `code_search_symbol` parity with
  `code_find_definition`).
- Note `code_find_references ⊇ code_callers` (references includes callers) in
  both docstrings so agents pick the narrower one deliberately.

### Task 9 (p3) — Standardize error/return conventions across sibling tools

`tk-standardize-error-return-conventions-across-sibl-cda8fc`

Apply **C8**:
- `workflow_project_block` self-block: return
  `{"blocked": false, "reason": "cannot block a project on itself"}` instead of
  raising `ValueError` (match `memory_link` self-link shape).
- `workflow_trace_mine` / `_annotate_trace` missing trace: return
  `{"slug": …, "error": "no such trace"}` (shaped) instead of raising, matching
  `memory_get`/`task_get` returning `None` for missing lookups. (Keep raising for
  genuinely malformed input.)
- Audit the remaining mutations for the same two patterns and align. Document the
  convention in a one-line "Errors" note in instructions.

### Task 10 (p3) — Resolve overlapping task/project mutation paths

`tk-resolve-overlapping-task-project-mutation-paths-33e08e`

Findings: `task_update(status="closed")` duplicates `task_close` (incl. the
unblock sweep); `task_update(parent=…)` duplicates `task_link(kind="parent")`;
`task_create(blocked_by=…)` overlaps `task_link`; `workflow_project_block/unblock/
get_blockers` reimplement task `blocked_by`/`ready` on a second node type.

Decisions (narrow the mutation surface, keep one obvious path per intent):
- **Closing**: `task_close` is the only documented close path. `task_update`
  drops `status="closed"` special-casing from its docstring and points to
  `task_close`; keep the code path working (so a raw `status="closed"` still
  unblocks) but stop advertising it. Do **not** duplicate the unblock logic in
  two docstrings.
- **Re-parenting**: keep `task_update(parent=…)` as the one re-parent path;
  `task_link` documents `kind="parent"` as "creates the same edge as
  `task_update(parent=…)`; prefer `task_update`." Single recommended path.
- **Blocking asymmetry**: document that `blocked_by` is set at `task_create` or
  via `task_link`, not `task_update` (which is the current reality) — state it
  rather than adding a redundant param.
- **Project vs task blocking**: clarify scope in docstrings — project-level
  blocking (`workflow_project_*`) is coarse cross-project sequencing; task-level
  blocking is fine-grained work ordering. They are deliberately separate node
  types; the docstrings say so and cross-reference, resolving the "which do I
  use" confusion without merging them.

## Sequencing

```
Task 2 ─┐
Task 1 ─┴─ (SEVERE, independent, do first)
Task 4 ── Task 5 ── (renames build on the C1/C2 id story)
Task 3 ── (docstring strip; touches every tool — land after renames to avoid churn)
Task 6 ── Task 7 (7 documents what 6 leaves behind)
Task 8, Task 9, Task 10 (independent p3s)
```

Practical order: **1, 2** (skills only, no code risk) → **4** (id story) →
**5** (renames) → **6** (remove wrappers + decision table) → **3** (bulk
docstring strip, now over final names) → **7, 8, 9, 10**.

## Verification (implementation phase)

- `uv run --group test pytest` green in both servers after each task
  (`uv sync --group test` first in a fresh checkout — the `test` group isn't
  auto-installed and a system `pytest` fails with `ModuleNotFoundError: fastmcp`).
- `ruff` clean.
- Round-trip smoke test for C2: definition → `symbol_id` → references resolves.
- Grep gates (must return nothing after the relevant task):
  `rg 'spec §|EDGE_PRECISION_TIERS|SYMBOL_TABLE|BRANCH_INDEXING|WITAN_RANK_DEFAULT_CONF'`
  over docstrings; `rg 'no in-place update|Supersedes tool yet'` over skills;
  `rg -w 'context_for_symbol|memory_symbol_context|project_memories|mine_trace|memory_get_project_facts|memory_list_patterns'`
  returns only the renamed/removed definitions' absence (i.e. old names gone from
  code and skills).

## Non-goals

- No change to the graph schema, storage, or query semantics beyond the C2 field
  alias.
- No new capability; this is purely surface/DX.
- Embeddings, Cedar/authz, deployment (tracked in other projects) are untouched.
