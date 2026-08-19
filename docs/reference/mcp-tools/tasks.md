<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: the registered FastMCP tool objects
-->

# Tasks

The work-coordination layer: what needs doing, what blocks what, and who holds which piece of work right now. `task_claim` is a **best-effort** compare-and-swap — it detects and rejects most lost races, but it is an advisory lease, not a hard lock. See [ADR 0003](../../explanation/decisions/0003-atomic-task-claims-cas.md) for what is and is not guaranteed.

## `task_create`

Create a task in the work-coordination graph.

Tasks are dependency-aware and hierarchical. Use ``parent`` to attach a
sub-issue to an ``epic`` (or any parent task); use ``blocked_by`` to record
dependencies so ``task_ready`` can withhold the task until its blockers
close.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `title` | str | **required** | Short label and full text of the work. |
| `description` | str | **required** | Short label and full text of the work. |
| `type` | `bug` \| `feature` \| `task` \| `chore` \| `epic` | `'task'` | ``bug`` \| ``feature`` \| ``task`` \| ``chore`` \| ``epic``. |
| `priority` | `p0` \| `p1` \| `p2` \| `p3` | `'p2'` | ``p0`` (highest) … ``p3``. Drives ``task_ready`` ordering. |
| `repo` | str? | `null` | Repo scoping — see instructions. |
| `project_slug` | str? | `null` | ``wp-`` slug of the WorkflowProject this task rolls up to. |
| `parent` | str? | `null` | ``tk-`` slug of the parent task/epic. Sets the hierarchy edge. |
| `blocked_by` | list[str]? | `null` | ``tk-`` slugs that must close before this task is ready. |
| `discovered_from` | list[str]? | `null` | ``tk-`` slugs of tasks during which this work was discovered. |
| `external_uri` | str? | `null` | A reference URI — e.g. a GitHub issue or PR. |
| `symbol_refs` | list[str]? | `null` | Code-graph symbol ids (``repo#path::Name``) this task concerns. |
| `tags` | list[str]? | `null` | Optional free-form tags. |

## `task_get`

Retrieve a single task by slug. Returns the full node or ``null``.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``tk-`` slug to retrieve. |

## `task_list`

List tasks, filtered by repo, status, project, parent, and/or assignee.

``project_slug`` and ``parent`` take precedence as the primary scope; other
filters are applied on top in Python. With no filters, lists recent tasks
across all repos.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Repo scoping — see instructions. |
| `status` | `open` \| `in_progress` \| `blocked` \| `closed`? | `null` | ``open`` \| ``in_progress`` \| ``blocked`` \| ``closed``. |
| `project_slug` | str? | `null` | List the tasks of a WorkflowProject. |
| `parent` | str? | `null` | List the direct children of a parent task/epic. |
| `assignee` | str? | `null` | Filter to a single owner. |

## `task_ready`

Return ready-to-work tasks: pickable tasks whose blockers are all closed.

A task is ready when its status is ``open``/``blocked`` (nobody is on it yet
and it is not closed), OR ``in_progress`` with an expired lease (the holder
likely abandoned it — see ``readiness.status_pickable``), AND every task in
its ``blocked_by`` list is closed. A returned ``in_progress`` task is
therefore a reclaim, not fresh work — check ``assignee``/``claimed_at``
(falling back to ``updated_at`` when ``claimed_at`` is null, e.g. a legacy
row) before starting it. This is the core coordination primitive — call it
to pick the next actionable item without manual triage. Results are
ordered by priority (``p0`` first).

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Repo scoping — see instructions. |
| `project_slug` | str? | `null` | Restrict to a single WorkflowProject. |
| `assignee` | str? | `null` | Restrict to a single owner (or pass to find your own ready work). |
| `limit` | int | `20` | Maximum tasks to return. Defaults to 20. |

## `task_update`

Update a task's mutable fields. Only non-null arguments are applied.

Use this to re-prioritise, re-parent (``parent``), reassign (``assignee``),
correct the repo association (``repo``), or attach an ``external_uri``. To
*claim* a task for work prefer ``task_claim`` (it sets ``in_progress`` + a
lease and refuses if someone else holds it); to close a task prefer
``task_close``; to add dependencies use ``task_link``.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``tk-`` slug of the task to update. |
| `title` | str? | `null` | New short label for the work. |
| `description` | str? | `null` | New full description. Replaces the existing text; it is not appended to. |
| `type` | `bug` \| `feature` \| `task` \| `chore` \| `epic`? | `null` | ``bug`` \| ``feature`` \| ``task`` \| ``chore`` \| ``epic``. |
| `status` | `open` \| `in_progress` \| `blocked` \| `closed`? | `null` | ``open`` \| ``in_progress`` \| ``blocked`` \| ``closed``. Two values have<br>side effects: ``in_progress`` stamps a fresh ``claimed_at`` lease, and<br>``closed`` stamps ``closed_at`` **and unblocks this task's dependents**,<br>exactly as ``task_close`` does. Prefer ``task_claim`` / ``task_close``<br>for those two transitions — they carry the ownership checks this does<br>not. |
| `priority` | `p0` \| `p1` \| `p2` \| `p3`? | `null` | ``p0`` (highest) … ``p3``. Drives ``task_ready`` ordering. |
| `repo` | str? | `null` | Canonical repo URI to (re)assign this task to. Pass an explicit value to<br>correct tasks that were created without proper repo context. |
| `assignee` | str? | `null` | Holder identity to reassign the task to. Prefer ``task_claim`` to take a<br>task for yourself — it checks nobody else holds it, which this does not. |
| `project_slug` | str? | `null` | ``wp-`` slug of the WorkflowProject this task rolls up to. |
| `parent` | str? | `null` | ``tk-`` slug of the parent task/epic. Written as both the<br>``parent_slug`` field and the ``ParentOf`` edge in one commit, so a<br>concurrent reader never sees the task parented one way and not the<br>other. **Re-parenting does not retract the previous edge**: the field<br>moves to the new parent while the old ``ParentOf`` remains, so a task<br>re-parented this way is reachable from both. Call<br>``task_unlink(kind="parent")`` on the old pair first if the edge<br>matters to you. |
| `external_uri` | str? | `null` | Reference URI — e.g. the GitHub issue or PR this task tracks. |
| `symbol_refs` | list[str]? | `null` | Code-graph symbol ids (``repo#path::Name``) this task concerns.<br>Replaces the existing list. |
| `tags` | list[str]? | `null` | Free-form tags. Replaces the existing list rather than merging into it. |

## `task_claim`

Claim a task for work: set it ``in_progress`` under ``assignee`` with a lease.

The coordination primitive for parallel/multi-user agents — call it before
starting a ready task so others see it is taken. Returns ``{"claimed": true,
…}`` on success, or ``{"claimed": false, "reason": …}`` when the task is
closed, still blocked, held by someone else (``"held"``/``"lost_race"``),
or the graph is too busy to complete the CAS after retrying
(``"contention"`` — safe and expected to retry). The lease (``claimed_at``)
expires if the holder never closes/releases, making the task reclaimable
(see ``task_ready``); re-calling renews it. Pass ``force`` to steal a live
claim.

BEST-EFFORT CAS — omnigraph 0.8.x exposes no conditional-write primitive, so
the claim write cannot be made atomic at the store. Instead we surface the
Lance optimistic-concurrency conflict (rather than masking it with a retry)
and re-read + post-write-verify ownership, so a lost race reports
``{"claimed": false, "reason": "lost_race", "held_by": …}`` instead of
silently clobbering the winner. The lease is the backstop for the residual
simultaneous-post-read case. True atomic CAS awaits an upstream omnigraph
conditional-write feature — see docs/adr/0003.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``tk-`` slug to claim. |
| `assignee` | str? | `null` | Holder identity. Defaults to the calling user (the JWT's<br>``preferred_username`` when deployed, the configured author locally)<br>qualified by ``session_id`` — see ``_claim_holder``. Two of one<br>person's parallel sessions must not share a holder string, or the<br>contention check passes and the second silently renews the first's<br>lease. Pass an explicit id to override (a worker name, a CI job). |
| `session_id` | str? | `null` | The calling agent session's id, which qualifies the default holder.<br>**Pass this when calling a deployed witan** — the server cannot infer<br>it (no shared environment, and MCP 2026-07-28 carries no session<br>state), and without it every one of your concurrent sessions claims<br>under the same name. The CLI's remote proxy fills it in automatically;<br>under local stdio the server falls back to its own<br>``$CLAUDE_SESSION_ID``, which it inherits from the agent. |
| `force` | bool | `False` | Steal the task even if another holder's lease is still valid. |

## `task_release`

Release a claim: clear the assignee/lease and return the task to ``open``.

Call when stepping away from an unfinished task so others can pick it up
(closing a finished task — use ``task_close`` — also ends the claim). Refuses
if the task is held by a different ``assignee`` unless ``force`` is set.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``tk-`` slug to release. |
| `assignee` | str? | `null` | Holder identity releasing the task. Defaults to the calling user, same<br>resolution as ``task_claim``'s ``assignee``. The held-by check compares<br>*identities*, ignoring the ``#<session>`` qualifier, so you can release a<br>claim one of your own other sessions took; another person's still needs<br>``force``. |
| `session_id` | str? | `null` | The calling agent session's id, same resolution and same reason as<br>``task_claim``'s. Only affects the holder string this call is compared<br>*as*; since the comparison is identity-level, omitting it against a<br>deployed server is harmless here in a way it is not for ``task_claim``. |
| `status` | `open` \| `in_progress` \| `blocked` \| `closed` | `'open'` | Status to return the task to (default ``open``). |
| `force` | bool | `False` | Release even if held by a different assignee. |

## `task_close`

Close a task: set status ``closed``, stamp ``closed_at``, record a resolution.

Closing a blocker is what unblocks its dependents — they become visible to
``task_ready`` once every blocker is closed.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``tk-`` slug to close. |
| `resolution` | str? | `null` | Short note on what was actually done. Worth writing: this is what a<br>later reader sees when asking why the task ended, and closing without<br>one leaves that unanswerable. |

## `task_link`

Link two tasks (or a task to a memory).

The meaning of ``from``/``to`` depends on ``kind``:
- ``blocks``          — ``from`` is the blocker, ``to`` is the blocked task.
  This is the only way to set a task's ``blocked_by`` after creation
  (``task_update`` does not).
- ``parent``          — ``from`` is the parent/epic, ``to`` is the child.
  Sets the same edge as ``task_update(parent=…)``; prefer ``task_update``.
- ``discovered_from`` — ``from`` is the new task, ``to`` is the source it came from.
- ``addresses``       — ``from`` is the task, ``to`` is a Memory slug it addresses.

For ``blocks`` and ``parent`` the denormalized ``blocked_by`` / ``parent_slug``
fields on the affected task are kept in sync so ``task_ready`` stays correct.

Reversible: ``task_unlink`` removes any of these, including one recorded in
the wrong direction.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `from_slug` | str | **required** | The ``tk-`` slug the edge points **from**. Its meaning depends on<br>``kind`` — see above; for ``blocks`` this is the blocker, for ``parent``<br>the epic, for ``discovered_from`` the newly-found task. |
| `to_slug` | str | **required** | The slug the edge points **to** — the blocked task, the child, the task<br>the work was discovered during, or (for ``addresses``) a ``Memory``<br>slug. |
| `kind` | `blocks` \| `parent` \| `discovered_from` \| `addresses` | **required** | ``blocks`` \| ``parent`` \| ``discovered_from`` \| ``addresses``. Getting<br>the direction wrong is the common mistake here, and it matters: for<br>``blocks`` it decides which task ``task_ready`` withholds. |

## `task_unlink`

Remove a link between two tasks (or a task and a memory) — the inverse of
``task_link``, with the same ``from``/``to`` meanings.

Use it when a link was recorded the wrong way round or against the wrong
slug. Removing a ``blocks`` link is how a task wrongly marked blocked
becomes ready again.

For ``blocks`` and ``parent`` the denormalized ``blocked_by`` /
``parent_slug`` fields are updated to match, so ``task_ready`` stays
correct. Unblocking a task whose remaining blockers are all closed returns
it from ``blocked`` to ``open`` — the mirror of what ``task_link`` does.

Returns ``{"from", "to", "kind", "removed"}``. ``removed`` is ``False``
when the edge was not there, which is not an error: calling this twice, or
on a link that never existed, is a safe no-op.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `from_slug` | str | **required** | The ``tk-`` slug the edge points **from** — the same direction it was<br>written with. Pass the endpoints as ``task_link`` received them, not<br>reversed. |
| `to_slug` | str | **required** | The slug the edge points **to**, or a ``Memory`` slug for<br>``kind="addresses"``. |
| `kind` | `blocks` \| `parent` \| `discovered_from` \| `addresses` | **required** | Which edge to remove: ``blocks`` \| ``parent`` \| ``discovered_from`` \|<br>``addresses``. |
