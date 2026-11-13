<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: the registered FastMCP tool objects
-->

# Tasks

The work-coordination layer: what needs doing, what blocks what, and who holds which piece of work right now. `task_claim` is a compare-and-swap with a lease, and every task write is fenced on the row it read, so a granted claim changes only by a write meant to change it (`force`, an explicit `task_update` of assignee or status). See [ADR 0003](../../explanation/decisions/0003-atomic-task-claims-cas.md) for what is and is not guaranteed.

## `task_create`

Create a task in the work-coordination graph.

Tasks are dependency-aware and hierarchical. Use ``parent`` to attach a
sub-issue to an ``epic`` (or any parent task); use ``blocked_by`` to record
dependencies so ``task_ready`` can withhold the task until its blockers
close.

Returns ``{"slug", "status", "repo", "similar"}``; ``similar`` is up to 3
existing tasks in any repo and status that match, a soft duplicate check.
If one is the same work, close this task as a duplicate.

*Writes (non-destructive).*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `title` | str | **required** | Short label and full text of the work. |
| `description` | str | **required** | Short label and full text of the work. |
| `type` | `bug` \| `feature` \| `task` \| `chore` \| `epic` | `'task'` | The kind of work; ``epic`` for a parent of sub-issues. |
| `priority` | `p0` \| `p1` \| `p2` \| `p3` | `'p2'` | ``p0`` is highest. Drives ``task_ready`` ordering. |
| `repo` | str? | `null` | Repo scoping; see instructions. |
| `project_slug` | str? | `null` | ``wp-`` slug of the WorkflowProject this task rolls up to. |
| `parent` | str? | `null` | ``tk-`` slug of the parent task/epic. Sets the hierarchy edge. |
| `blocked_by` | list[str]? | `null` | ``tk-`` slugs that must close before this task is ready. |
| `discovered_from` | list[str]? | `null` | ``tk-`` slugs of tasks during which this work was discovered. |
| `external_uri` | str? | `null` | A reference URI — e.g. a GitHub issue or PR. |
| `symbol_refs` | list[str]? | `null` | Symbol ids this task concerns. |
| `tags` | list[str]? | `null` | Optional free-form tags. |
| `all_repos` | bool | `False` | Create the task under no repo, so every repo's task reads include it. |

## `task_get`

Retrieve a single task by slug. Returns the full node or ``null``.

The node carries a ``comments`` list — attributed, append-only notes left by
``task_comment``, oldest first. READ THEM BEFORE EXECUTING: a comment is how
another agent says the task's stated premise is wrong without overwriting
your description, so it is often a correction to the very plan the
description sets out.

It also carries ``blocks`` (the tasks this one holds back), ``children``
and ``branches`` (the ``CodeBranch``es working it), and may carry
``lease_expired``, ``live_pr`` and ``ui_url`` (see instructions).

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``tk-`` slug to retrieve. |

## `task_list`

List tasks, filtered by repo, status, project, parent, and/or assignee.

``project_slug`` and ``parent`` are the primary scope; other filters
apply on top. ``closed_at`` is set exactly when status is ``closed``.
``first_claimed_at`` is the first claim ever and survives release, close
and reopen; ``claimed_at`` is the current lease.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Repo scoping; see instructions. |
| `status` | `open` \| `in_progress` \| `blocked` \| `closed`? | `null` | Filter to one status. |
| `project_slug` | str? | `null` | List the tasks of a WorkflowProject. |
| `parent` | str? | `null` | List the direct children of a parent task/epic. |
| `assignee` | str? | `null` | Filter to one holder; ``"@me"`` is you (every session). |
| `limit` | int? | `null` | Max rows, 1 to 10,000. Omitted: 50 unscoped, all when scoped by repo,<br>project or parent. On the default repo scope, rows are this repo's<br>then the unscoped ones, so a limit may drop the unscoped ones. |
| `all_repos` | bool | `False` | List every repo's tasks, in one ``updated_at`` order. |

## `task_search`

Plain BM25 text search over tasks (no graph expansion).

Returns up to ``limit`` tasks ranked by relevance over description and
title. Run it before ``task_create`` to check the work is not already
tracked. For "the next task" in a project use
``task_ready(project_slug=...)``.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | str | **required** | Free-text search query. |
| `repo` | str? | `null` | Repo scoping; see instructions. Includes unscoped tasks. Ignored with<br>``project_slug``. |
| `status` | `open` \| `in_progress` \| `blocked` \| `closed`? | `null` | Filter to one status. Omit to include closed tasks, which can show the<br>work is already done. |
| `project_slug` | str? | `null` | Search only this project's tasks, in every repo. |
| `assignee` | str? | `null` | Filter to one holder; ``"@me"`` is you. |
| `limit` | int | `20` | Max hits, 1 to 10,000, applied after every filter. |
| `all_repos` | bool | `False` | Search every repo's tasks. |

## `task_ready`

Return ready-to-work tasks: pickable tasks whose blockers are all closed.

Call it to pick the next item. A task is ready when it is ``open`` or
``blocked``, or ``in_progress`` with an expired lease, and every task in
its ``blocked_by`` is closed. An ``in_progress`` result is a reclaim
(``lease_expired: true``): check ``assignee`` and ``claimed_at`` (or
``updated_at`` when that is null) first.
Ordered by priority, ``p0`` first.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `repo` | str? | `null` | Repo scoping; see instructions. |
| `project_slug` | str? | `null` | Restrict to a single WorkflowProject. |
| `assignee` | str? | `null` | Filter to one holder. |
| `limit` | int | `20` | Maximum tasks to return. |
| `all_repos` | bool | `False` | Return every repo's ready tasks. |

## `task_update`

Update a task's mutable fields. Only non-null arguments are applied.

Re-prioritise, re-parent, reassign, fix the repo, or attach an
``external_uri``. To take a task use ``task_claim``, to finish one
``task_close``, to add a dependency ``task_link``.

*Writes (destructive).*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``tk-`` slug of the task to update. |
| `title` | str? | `null` | New short label for the work. |
| `description` | str? | `null` | Replacement description (not appended). |
| `type` | `bug` \| `feature` \| `task` \| `chore` \| `epic`? | `null` | The kind of work. |
| `status` | `open` \| `in_progress` \| `blocked` \| `closed`? | `null` | ``in_progress`` stamps a fresh lease, filling a missing assignee with<br>you, without ``task_claim``'s checks. ``closed`` unblocks dependents<br>and is refused while another person holds a live claim (see<br>``force``). Prefer ``task_claim``/``task_close`` for those two. |
| `priority` | `p0` \| `p1` \| `p2` \| `p3`? | `null` | ``p0`` is highest. Drives ``task_ready`` ordering. |
| `repo` | str? | `null` | Repo URI to reassign the task to. |
| `assignee` | str? | `null` | Reassign to this holder. Unlike ``task_claim``, does not check who<br>holds the task. |
| `project_slug` | str? | `null` | ``wp-`` slug of the WorkflowProject this task rolls up to. |
| `parent` | str? | `null` | ``tk-`` slug of the parent task. Re-parenting leaves the old<br>``ParentOf`` edge; remove it with ``task_link(kind="parent",<br>remove=True)``. |
| `external_uri` | str? | `null` | Reference URI, e.g. the GitHub issue or PR this task tracks. |
| `symbol_refs` | list[str]? | `null` | Symbol ids this task concerns. Replaces the list. |
| `tags` | list[str]? | `null` | Free-form tags. Replaces the list. |
| `session_id` | str? | `null` | Your agent session id, for the assignee ``in_progress`` fills in; see<br>instructions. |
| `force` | bool | `False` | With ``status="closed"``, close despite another person's live claim. |

## `task_claim`

Claim a task for work: set it ``in_progress`` under ``assignee`` with a lease.

Call it before starting a ready task so others see it is taken.
Re-calling renews the lease; an expired lease makes the task reclaimable.
Returns ``{"claimed": true, ...}``, or ``{"claimed": false, "reason":
...}`` when the task is closed, blocked, held (``held``/``lost_race``), or
the graph stayed too busy (``contention``, safe to retry).

A first claim also returns ``memories``, up to three teammates' notes the
task links to or matches (read them before the first edit; empty does not
mean nothing exists, ``recall`` searches wider), and ``overlaps``, other
in-flight tasks sharing a symbol or file in ``symbol_refs``. A shared
symbol is a likely merge conflict: read that task first and
``task_comment`` it if the plans collide. A ``warning`` key means a
deployed call named no session, so your parallel sessions share one
holder.

*Writes (non-destructive).*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``tk-`` slug to claim. |
| `assignee` | str? | `null` | Holder identity; defaults to you, qualified by ``session_id``.<br>Override for a worker or CI job. |
| `session_id` | str? | `null` | Your agent session id; see instructions. Without it, a deployed witan<br>records your parallel sessions under one holder. |
| `force` | bool | `False` | Steal the task even if another holder's lease is still valid. |
| `repo` | str? | `null` | Repo scoping; used only to link your branch, not to scope the claim. |
| `branch` | str? | `null` | Your git branch, linked to the task. witan's CLI proxy fills it in;<br>under local stdio it defaults to the server's checkout. |

## `task_release`

Release a claim: clear the assignee/lease and return the task to ``open``.

Call when stepping away from an unfinished task so others can pick it up
(closing a finished task — use ``task_close`` — also ends the claim). Refuses
if the task is held by a different ``assignee`` unless ``force`` is set.

*Writes (destructive).*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``tk-`` slug to release. |
| `assignee` | str? | `null` | Holder identity releasing it; defaults to you. Any of your sessions'<br>claims count as yours; another person's needs ``force``. |
| `session_id` | str? | `null` | Your agent session id; see instructions. |
| `status` | `open` \| `in_progress` \| `blocked` \| `closed` | `'open'` | Status to return the task to. |
| `force` | bool | `False` | Release even if held by a different assignee. |

## `task_close`

Close a task: set status ``closed``, stamp ``closed_at``, record a resolution.

Closing a blocker unblocks its dependents. Refuses, writing nothing, while
another person holds a live claim: ``{"closed": false, "reason": "held",
"held_by", "claimed_at", "remedy"}``. Claims held by any of your own
sessions, expired leases and unheld tasks close. A retry of a close that
landed keeps its ``closed_at``. A ``warning`` names dependents that could
not be reopened; ``task_ready`` lists them anyway.

*Writes (destructive, idempotent).*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``tk-`` slug to close. |
| `resolution` | str? | `null` | What was actually done: what a later reader sees when asking why the<br>task ended. |
| `assignee` | str? | `null` | Holder identity closing it; defaults to you. Pass it when the claim<br>was taken under an explicit name (a worker, a CI job). |
| `force` | bool | `False` | Close even while another person's claim is live. |

## `task_comment`

Leave an attributed, append-only comment ON a task without changing it.

THIS IS THE TOOL FOR SAYING SOMETHING *ABOUT* SOMEONE ELSE'S TASK. Reach for
it when you have found a problem with a task you are not executing — its
premise is wrong, its mechanism cannot fire, the work is already done
elsewhere. The alternatives are both worse: ``task_update`` overwrites the
other author's description, and ``task_create`` puts an item that is not
work into everyone's ready-work list.

The comment is attributed to you and stamped, and appears in ``task_get`` —
and, for a task the holder's session is actively working, in their injected
context. Flat and permanent: no threading, no editing, no deleting.

NOT for your own execution notes on your own task (that is the task's
``description`` or ``resolution``), except the one-line ``checkpoint``
the witan-task skill has a holder leave after each durable step so a
cut-off run can resume. And not for reusable repo knowledge (that is
``memory_store`` — a comment is read once by one executor).

*Writes (non-destructive).*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `slug` | str | **required** | The ``tk-`` slug to comment on. |
| `body` | str | **required** | The comment text. Say the thing itself, with evidence — the holder is<br>mid-execution and will act on this instead of the description. |

## `task_link`

Link two tasks (or a task to a memory), or with ``remove=True`` unlink them.

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

``remove=True`` removes a link, passing the endpoints as it was written.
Removing a ``blocks`` link returns the task from ``blocked`` to ``open``
once its remaining blockers are closed. Removing an absent link returns
``removed: False``, not an error.

*Writes (destructive).*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `from_slug` | str | **required** | The ``tk-`` slug the edge points from; see ``kind`` above. |
| `to_slug` | str | **required** | The slug the edge points to; a Memory slug for ``addresses``. |
| `kind` | `blocks` \| `parent` \| `discovered_from` \| `addresses` | **required** | The edge kind. Direction matters: for ``blocks`` it decides which task<br>``task_ready`` withholds. |
| `remove` | bool | `False` | Remove the ``from -> to`` edge of ``kind`` instead of adding it. |

## `task_for_branch`

Return the tasks linked to a git branch — "what is this branch already for".

Only a successful ``task_claim`` writes the link, so a non-empty result
means somebody holds a task on this branch. Call it before starting fresh
work on a branch you did not create, and continue or coordinate rather
than open a second task. An empty result does not mean nobody is working
the branch. Closed tasks are included.

*Read-only.*

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `branch` | str | **required** | Git branch name. Required: a deployed server cannot infer it. |
| `repo` | str? | `null` | Repo scoping; see instructions. |
