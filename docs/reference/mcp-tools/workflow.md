<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: the registered FastMCP tool objects
-->

# Workflow projects & sessions

Tracking an engineering objective across many agent sessions without an explicit hand-off. A project spans phases and repos; each session links itself to one, and a completed project leaves a trace behind for later pattern-mining.

## `workflow_project_create`

Create a new workflow project to track an engineering objective.

Call this at the start of a multi-session project before calling
``workflow_session_start``. The returned slug is used to link sessions.

A project may span several repos (a service that touches a Django app, its
frontend, and the infra repo that deploys it) or none (a cross-cutting
objective not yet tied to any repo). The repo set also grows automatically
as sessions run in new repos — see ``workflow_session_start``.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `title` | str | **required** | Short name for the project. Used in listings and injected context. |
| `description` | str | **required** | Full description of the objective — what will be built or changed and why. |
| `phase` | `discovery` \| `spec` \| `implementation` \| `delivery` | `'discovery'` | Starting phase. One of ``discovery``, ``spec``, ``implementation``,<br>``delivery``. Defaults to ``discovery``. |
| `repos` | list[str]? | `null` | Canonical repo URIs this project spans. The current repo (detected from<br>``.git/config``) is added automatically. Omit entirely when creating a<br>repo-less "floating" project from outside any git repo. Guessing here is<br>fine — a project's real blast radius is rarely known at creation. The<br>set can be corrected at any time with ``workflow_project_update``<br>(``repos`` to replace it, ``add_repos``/``remove_repos`` to nudge it),<br>and it also grows on its own as sessions run in new repos. |
| `github_issue` | str? | `null` | URL of the GitHub issue tracking this work.<br>e.g. ``github.com/mitodl/ol-django/issues/847``. |
| `tags` | list[str]? | `null` | Optional list of tags for grouping and searching. |

## `workflow_project_get`

Retrieve a single workflow project by slug.

Returns the full project node (including ``blocked_by`` and ``blocks``
lists) or ``null`` if not found.

``blocked_by`` lists the ``wp-`` slugs of projects that must complete
before this one is ready. ``blocks`` lists projects this project is
currently blocking (derived by scanning all active projects).

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``wp-`` slug to retrieve. |

## `workflow_project_status`

One-call "what should I do next" resume view for a workflow project.

Combines the four things an agent (or the context hook) needs to pick up a
project without re-deriving state: current **phase**, the **ready tasks**
under it (same readiness rule as ``task_ready``), the **last session's
handoff summary** (and whether it's still open), and any project-level
**blockers**. Returns ``None`` if the project doesn't exist.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``wp-`` slug of the project to resume. |

## `workflow_project_list`

List workflow projects, optionally filtered by repo, status, and phase.

Defaults to listing only ``active`` projects. Pass ``status=None`` to see
all statuses. The ``UserPromptSubmit`` hook calls this to inject project
context into new sessions.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Canonical repo URI to filter to (membership test against each<br>project's repo set). Auto-detected from ``.git/config`` if omitted.<br>Pass an empty string to list projects across all repos. |
| `status` | `active` \| `completed` \| `abandoned`? | `'active'` | ``active`` \| ``completed`` \| ``abandoned`` \| ``None`` for all.<br>Defaults to ``active``. |
| `phase` | `discovery` \| `spec` \| `implementation` \| `delivery`? | `null` | Optional phase filter applied after fetching. |
| `ready` | bool | `False` | When ``True``, only return active projects whose blockers are all<br>completed (i.e. projects that are unblocked and actionable). |

## `workflow_project_update`

Correct a project's metadata after creation.

The escape hatch for everything ``workflow_project_advance`` (phase,
``github_pr``), ``workflow_project_complete`` (completion) and
``workflow_project_block``/``_unblock`` (dependencies) don't cover. Every
parameter is optional and only what you pass is touched; omitting a field
leaves it exactly as it was, so this can never blank something by accident.
Returns the updated project, or ``None`` if the slug doesn't exist.

The common case is repos. A project's real blast radius is rarely known
during discovery, and until the set is right, repo-scoped recall from the
repos where the work actually lands won't surface the project at all. Pass
``repos`` to replace the set wholesale, or ``add_repos``/``remove_repos`` to
nudge it (both may be passed together; removals are applied after
additions). Repos are a plain list field on the project node, not edges, so
a removal really removes — unlike ``workflow_project_unblock``, which can
only update its denormalized field because omnigraph edges are append-only.

Two things this deliberately can't do:

- **Set the phase.** ``workflow_project_advance`` stays the only route, so
  a transition is always seen by its ordering check. It does allow going
  backwards (with a confirmation), which is how a phase set in error gets
  corrected — this tool would just bypass the prompt.
- **Complete a project.** ``status`` accepts ``abandoned`` (for work that
  stopped without an outcome) and ``active`` (to revive it), but not
  ``completed``: that belongs to ``workflow_project_complete``, which seals
  a corpus trace. Nothing should mint a trace without a narrative.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``wp-`` slug of the project to update. |
| `title` | str? | `null` | Replacement short name. |
| `description` | str? | `null` | Replacement description. |
| `repos` | list[str]? | `null` | Replace the repo set wholesale with these canonical URIs. |
| `add_repos` | list[str]? | `null` | Canonical repo URIs to add to the set. |
| `remove_repos` | list[str]? | `null` | Canonical repo URIs to drop from the set. |
| `tags` | list[str]? | `null` | Replacement tag list. Pass ``[]`` to clear. |
| `github_issue` | str? | `null` | URL of the GitHub issue tracking this work. |
| `status` | `active` \| `abandoned`? | `null` | ``active`` \| ``abandoned``. |

## `workflow_project_advance`

Advance a workflow project to the next phase.

Call when transitioning from e.g. spec to implementation. Optionally
record a PR URL when moving to or through the delivery phase.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``wp-`` slug of the project to update. |
| `phase` | `discovery` \| `spec` \| `implementation` \| `delivery` | **required** | New phase: ``discovery`` \| ``spec`` \| ``implementation`` \| ``delivery``. |
| `github_pr` | str? | `null` | URL of the GitHub PR if one has been opened. |

## `workflow_project_complete`

Mark a workflow project as completed and assemble its corpus trace.

This creates a ``WorkflowTrace`` node that aggregates all linked sessions
into an immutable record for later pattern mining. Idempotent: if a trace
already exists for this project, it is returned without re-inserting.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``wp-`` slug of the project to complete. |
| `outcome` | str | **required** | Free-text narrative of what was delivered. Be specific — this is<br>the primary content of the corpus record. |
| `github_pr` | str? | `null` | URL of the merged PR, if applicable. |

## `workflow_project_block`

Declare that one project must complete before another can begin.

Project-level sequencing — coarse ordering between whole projects. For
fine-grained ordering between individual tasks use ``task_link(kind="blocks")``;
the two are deliberately separate (Project vs Task nodes).

Adds a ``ProjectBlocks`` graph edge (``slug`` → ``blocks_slug``) and
appends ``slug`` to ``blocks_slug.blocked_by`` so the ready-work check
in ``workflow_project_list`` can filter without traversing edges.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``wp-`` slug of the *blocking* project (must finish first). |
| `blocks_slug` | str | **required** | The ``wp-`` slug of the project being *blocked*. |

## `workflow_project_unblock`

Remove a project dependency declared with ``workflow_project_block``.

Removes ``slug`` from ``blocks_slug.blocked_by`` AND deletes the
``ProjectBlocks`` edge, so the graph and the denormalized field (which
drives the ready-work check) agree.

Earlier versions left the edge in place, on the belief that omnigraph
edges were append-only. They are not — see ``_unlink_edge``.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``wp-`` slug of the *blocking* project to remove. |
| `blocks_slug` | str | **required** | The ``wp-`` slug of the project to unblock. |

## `workflow_project_get_blockers`

Return all projects that are blocking the given project.

Resolves each slug in the project's ``blocked_by`` list and returns the
full node for each blocker, including its current status.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``wp-`` slug of the project to check. |

## `workflow_project_link_memory`

Link a memory to a workflow project (the ``Informed`` edge).

Records that a project consulted or produced a memory — a ``pattern``,
``lesson``, ``project_fact``, or ``agent_context``. The linked memories
surface when the project's corpus trace is mined for reusable patterns.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `project_slug` | str | **required** | The ``wp-`` slug of the project. |
| `memory_slug` | str | **required** | The ``pat-`` / ``les-`` / ``pf-`` / ``ctx-`` slug returned by ``memory_store``. |

## `workflow_project_memories`

"What did we learn during project X" — the provenance walk.

Assembles the memories connected to a project from two grains:
- **session-grain** (``SessionProduced``): memories the project's sessions
  created, auto-recorded by ``memory_store`` when a session is active;
- **project-grain** (``Informed``): memories explicitly linked via
  ``workflow_project_link_memory``.

De-duplicated by slug. The flat ``memories`` list is assembled with two
queries regardless of session count. Pass ``group_by_session=True`` to also
get a ``by_session`` breakdown — that costs one extra query per session, so
it is opt-in.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `project_slug` | str | **required** | The ``wp-`` slug whose memories to assemble. |
| `group_by_session` | bool | `False` | Also return a ``by_session`` breakdown of which session produced what.<br>Costs one extra query per session, so leave it off unless you need the<br>attribution. |

## `workflow_session_start`

Link the current agent session to a workflow project.

Call this at the start of any session that is contributing to a tracked
project. The context injected by the context-injection hook (Claude Code) or
extension (Pi) provides the ``project_slug``; ``session_id`` should be the
session id — ``$CLAUDE_SESSION_ID`` on Claude Code, or any stable unique
string for the session otherwise.

Returns an explicit session handle (``session_slug``, ``project_slug``,
``phase``, ``session_id``, ``started_at``). Hold on to it and pass
``session_slug`` back to ``workflow_session_end`` — the handle is the only
thing that ties the two calls together, since the protocol carries no session
state of its own and consecutive calls may land on different replicas.

**Re-entrant.** Calling again for a (``project_slug``, ``session_id``) whose
session is still open returns that same handle with ``existed: true``
instead of minting a second node — so a hook retry, a transport reconnect,
or the replica failover the paragraph above warns about can't silently
duplicate a session. Any newly-supplied ``repo`` and ``tags`` are merged
into the existing session; ``phase`` is left at what the first call set (use
``workflow_project_advance`` to move a project's phase). Once a session has
been ended, the same ``session_id`` starts a fresh session — one
``$CLAUDE_SESSION_ID`` legitimately spans several working stints.

Two *simultaneous* starts (a client retrying while the first request is
still in flight) can still both insert, since the check and the insert are
not one atomic operation. That is resolved immediately after the fact rather
than left for the migration to find — see ``_dedupe_open_sessions``. Both
racers return the same handle.

Because the repo accretion below runs on the re-entrant path too, calling
this once per repo remains a valid way to widen a project's repo set — but
``workflow_project_update(add_repos=[...])`` does it directly, without
needing a session at all.

When a repo is detected and the checkout is on a git branch, also
upserts a ``CodeBranch`` (repo, branch) and links it ``ForProject`` to
this project — schema.pg § Code Branches. Best-effort: silently skipped
with no repo/branch context, never fails the session start.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `project_slug` | str | **required** | The ``wp-`` slug of the project this session belongs to. |
| `session_id` | str | **required** | Unique identifier for this agent session. |
| `phase` | `discovery` \| `spec` \| `implementation` \| `delivery` | **required** | The phase this session is working in. |
| `repo` | str? | `null` | Repo scoping — see instructions. |
| `tags` | list[str]? | `null` | Optional tags. |

## `workflow_session_end`

Close the current session with a summary of work accomplished.

Call this before ending a session to produce a high-quality corpus record.
The ``Stop`` hook will auto-close sessions that did not call this, but
with a placeholder summary.

For best corpus quality, write a summary that includes:
- What was done this session
- What remains for the next session
- Any blockers or decisions made

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `session_slug` | str | **required** | The ``ws-`` slug returned by ``workflow_session_start``. |
| `summary` | str | **required** | Description of what was accomplished and what remains. |
| `tools_used` | list[str]? | `null` | List of tool names used. e.g. ``["Edit", "Bash", "Read"]``. |
| `files_changed` | list[str]? | `null` | List of file paths modified in this session. |

## `workflow_session_list`

List workflow sessions, newest last.

Mainly for finding sessions that leaked open — one whose agent died, or
whose Stop hook could not reach the graph. An open session is not cosmetic:
``workflow_project_complete`` folds every linked session into the corpus
trace, so one with no ``ended_at`` inflates ``session_count``, contributes
its phase while having recorded nothing, carries no handoff summary, and
cannot extend ``duration`` (computed from ``max(ended_at)``). It is also
what drives the context hook's "N sessions in <phase>" staleness nag.

Use ``witan session sweep`` to close them in bulk.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `project_slug` | str? | `null` | Restrict to one project's sessions. Omit for every project. |
| `open_only` | bool | `False` | Only sessions with no ``ended_at``. Superseded sessions (deduped by<br>``witan migrate dedupe-sessions``) are always excluded — they are<br>already skipped by every aggregate read and are not leaks. |

## `workflow_trace_list`

List corpus WorkflowTrace records, optionally filtered by repo, tags, or author.

Traces are otherwise only reachable by slug (``wt-<project-slug>``) via
``get_trace`` — this is the discovery path for browsing or mining across
many completed projects (e.g. as onboarding case studies of how a project
went end to end).

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Canonical repo URI to filter to (membership test against each trace's<br>repo set). Auto-detected from ``.git/config`` if omitted. Pass an<br>empty string to list traces across all repos. |
| `tags` | list[str]? | `null` | Only return traces that carry ALL of these tags. |
| `author` | str? | `null` | Only return traces created by this author. |
| `limit` | int | `50` | Max rows to return (applied after filtering). |

## `workflow_trace_get`

Retrieve a single corpus WorkflowTrace by slug.

Slug handling is simple: a slug already starting with ``wt-`` is used as-is;
**any other** slug is prefixed with ``wt-``. So the trace slug
(``wt-<project-slug>``) and the project slug (``wp-<project-slug>``, which
becomes ``wt-wp-<project-slug>``) both resolve to the same trace, and callers
never have to hand-construct the ``wt-`` slug (the fragile step the
``witan-project-tracker`` skill used to instruct). Returns the full trace
node (title/description/outcome, ``session_count``, ``phases``, ``duration``,
and any mined ``lessons_slug``/``patterns_slug``) or ``None`` if no trace
exists — a project only has a trace once ``workflow_project_complete`` has
sealed it.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``wt-`` trace slug, or the ``wp-`` project slug it was minted from<br>(anything not already ``wt-``-prefixed gets the prefix added). |

## `workflow_trace_annotate`

Append lesson/pattern memory slugs to an existing WorkflowTrace.

Lets an agent (or ``workflow_trace_mine``) record which Memory nodes a completed
project's trace produced without re-running ``workflow_project_complete``
(traces are otherwise immutable after creation). Unions with whatever is
already recorded, so it's safe to call repeatedly as more lessons/patterns
are mined over time.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `trace_slug` | str | **required** | The ``wt-`` slug of the trace to annotate. |
| `lessons_slug` | list[str]? | `null` | ``les-`` memory slugs to add to the trace's ``lessons_slug`` field. |
| `patterns_slug` | list[str]? | `null` | ``pat-`` memory slugs to add to the trace's ``patterns_slug`` field. |

## `workflow_trace_mine`

Turn a completed WorkflowTrace into reusable Pattern/Lesson Memory nodes.

Call with no ``patterns``/``lessons`` first — returns the trace itself
(title, description, outcome) plus every session summary from its project,
the raw material to mine for reusable knowledge. Review that, then call
again with the patterns/lessons you propose to persist them: each becomes
a ``Memory`` node, gets an ``Informed`` edge back to the trace's project,
and its slug is appended to the trace's ``lessons_slug``/``patterns_slug``
fields.

These mined memories are read by other agents for self-improvement, but
are equally a corpus of worked examples for people onboarding onto this
system — write ``title``/``content`` so a newcomer unfamiliar with the
project can follow the reasoning, not just a terse note for a future agent.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `trace_slug` | str | **required** | The ``wt-`` slug of the trace to mine. |
| `patterns` | list[object]? | `null` | Proposed pattern memories to create on this call. Each dict needs<br>``title`` and ``content``; may also include ``repo``, ``language``,<br>and ``tags``. |
| `lessons` | list[object]? | `null` | Proposed lesson memories to create on this call. Each dict needs<br>``title`` and ``content``; may also include ``repo``, ``severity``,<br>and ``tags``. |
| `session_slug` | str? | `null` | The ``ws-`` handle from ``workflow_session_start``, recorded as the<br>provenance of every memory mined on this call — see ``memory_store``. |
