"""Logic for the inject-context and session-checkpoint CLI commands.

Both commands are invoked as Claude Code hooks and must be completely
fault-tolerant — they never raise, never block, and never produce unexpected
output. Porting these from shell scripts into Python lets the hooks be
one-liners (``witan inject-context`` / ``witan session-checkpoint``) that
work regardless of whether witan was installed from a checkout or via uvx.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from witan_core.observability import get_logger

from . import readiness, session_state
from . import repo as repo_module
from .graph import OmnigraphClient

logger = get_logger("witan.context")

# The context hook runs a fresh process on every prompt, so an in-process cache
# (like witan-code's server-side ``_cached_git``) can't help it. A tiny on-disk
# cache keyed by project dir amortizes the git subprocesses across a burst of
# prompts instead. Short TTL so a branch switch is picked up almost immediately.
_REPO_CACHE_TTL = 5.0

# Each omnigraph read is a full store scan (~1-2s on a large graph), and the hook
# issues several per prompt — so a burst of prompts (e.g. picking a skill via a
# `/` command) can each pay multiple seconds and blow the hook timeout. Cache the
# *rendered* block on disk with a short TTL so only the first prompt in a window
# pays the cost; the rest read one small file. The content is advisory, so a few
# seconds of staleness is fine. Override with WITAN_CONTEXT_TTL (seconds; 0
# disables).
_OUTPUT_CACHE_TTL = 30.0


def _output_cache_ttl() -> float:
    raw = os.environ.get("WITAN_CONTEXT_TTL")
    if raw is None:
        return _OUTPUT_CACHE_TTL
    try:
        return max(0.0, float(raw))
    except ValueError:
        # Warning, not debug: this is a misconfiguration a human should fix, and
        # it can only fire for someone who set the variable to a non-number, so
        # it cannot become per-prompt noise.
        logger.warning(
            "witan.context.bad_ttl", value=raw, falling_back_to=_OUTPUT_CACHE_TTL
        )
        return _OUTPUT_CACHE_TTL


def _atomic_write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically and privately; never raises.

    The hook runs as concurrent fresh processes (a ``/``-command burst fires it
    repeatedly), so a plain ``write_text`` could let one process read a half-
    written file. Writing a process-unique temp file and ``os.replace``-ing it in
    means a reader always sees either the old or the new *complete* file. The
    temp file is created ``0600`` (umask-independent) because the cached block
    contains project/task titles and lives in a shared temp dir.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, text.encode())
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError:
        # Debug: the cache is advisory, and the caller recomputes. Anything
        # louder would fire on every prompt for a read-only temp dir.
        logger.debug("witan.context.cache_write_failed", path=str(path), exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            # Nothing left to try, and the temp file is in a temp dir. Silence
            # is right here: reporting a failed cleanup of a failed write would
            # be two lines about the same non-event.
            pass


def _output_cache_file(graph_uri: str, repo: str | None, branch: str | None) -> Path:
    key = f"{graph_uri}|{repo}|{branch}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return session_state.session_state_dir() / f"witan-ctx-{digest}.json"


# ── Delivered-comment record ──────────────────────────────────────────────────
#
# A comment on a task you hold should interrupt once, not every prompt until you
# release the task — so the hook needs to know what it has already shown you.
#
# Kept in a local file rather than as read receipts in the graph, deliberately.
# "Read" here means "was rendered into this machine's prompt", which is a fact
# about a client, not about the shared work graph; storing it there would make
# every prompt a write, and would still be answering the wrong question for any
# other reader. Losing the file re-shows a comment, which is the harmless
# direction to fail in.
#
# A SET OF SLUGS, NOT A TIMESTAMP WATERMARK. `created_at` orders comments but is
# not an arrival sequence: `store_merge` reconciles `TaskComment` like any other
# type (`_RECONCILE_TS_FIELDS`), so a comment written at 09:00 against another
# store can land here after 10:00 has already been marked delivered. A watermark
# would silently swallow it forever — the one direction this file must not fail
# in. Membership has no such ordering assumption.


def _seen_file(cache_key: str) -> Path:
    digest = hashlib.sha1(cache_key.encode()).hexdigest()[:16]
    return session_state.session_state_dir() / f"witan-seen-{digest}.json"


def _read_seen(cache_key: str) -> dict[str, list[str]]:
    """``{task_slug: [comment slugs already shown]}``; ``{}`` if unusable.

    A pre-slug file (values were a timestamp string) reads as ``{}`` for those
    tasks rather than being migrated: the whole record is one prompt's worth of
    re-shown comments, and a migration would have to invent slugs it cannot know.
    """
    try:
        data = json.loads(_seen_file(cache_key).read_text())
    except Exception:  # noqa: BLE001 — missing/corrupt record → show everything
        logger.debug("witan.context.seen_read_failed", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v
        for k, v in data.items()
        if isinstance(v, list) and all(isinstance(s, str) for s in v)
    }


def _write_seen(cache_key: str, seen: dict[str, list[str]]) -> None:
    _atomic_write_private(_seen_file(cache_key), json.dumps(seen))


def _read_output_cache(
    graph_uri: str, repo: str | None, branch: str | None
) -> str | None:
    ttl = _output_cache_ttl()
    if ttl <= 0:
        return None
    try:
        data = json.loads(_output_cache_file(graph_uri, repo, branch).read_text())
        if time.time() - data["stamp"] < ttl:
            return data["output"]
    except Exception:  # noqa: BLE001 — missing/corrupt/stale cache → recompute
        # Debug, deliberately: a *missing* cache is the normal first-prompt
        # case, so this path is expected rather than degraded.
        logger.debug("witan.context.output_cache_miss", exc_info=True)
        return None
    return None


def _write_output_cache(
    graph_uri: str, repo: str | None, branch: str | None, output: str
) -> None:
    if _output_cache_ttl() <= 0:
        return
    _atomic_write_private(
        _output_cache_file(graph_uri, repo, branch),
        json.dumps({"stamp": time.time(), "output": output}),
    )


# ── Context injection (UserPromptSubmit hook) ─────────────────────────────────


def _stale_repo_case_present(
    repo: str, task_rows: list[dict], project_rows: list[dict]
) -> bool:
    """True if ``task_rows``/``project_rows`` — reads the hook already made —
    contain a repo value that ``witan migrate repo-keys`` would rewrite.

    ``repo`` is always canonical here (``_detect_repo`` routes through
    ``repo_module.normalise``), so a stored value is stale exactly when
    normalising it yields ``repo`` but the stored value itself differs —
    matching the migration's own "needs rewriting" check
    (``server.migrate_repo_keys``). A plain case-insensitive string compare
    would also flag a self-hosted repo whose path case is *not* folded by
    ``normalise`` (only github.com/gitlab.com paths are) — a value that
    happens to case-insensitively match but is left alone by the migration,
    and may even be a genuinely different, case-sensitive-path repo. Best
    -effort: only scans the rows already fetched for this prompt, not the
    whole store, so a store with no active projects/tasks for this repo may
    miss stale memories — the migration command itself is the exhaustive
    check.
    """
    for row in task_rows:
        value = row.get("repo")
        if value and value != repo and repo_module.normalise(value) == repo:
            return True
    for project in project_rows:
        for value in project.get("repos") or []:
            if value != repo and repo_module.normalise(value) == repo:
                return True
    return False


def _dbg(enabled: bool, msg: str) -> None:
    """Emit a diagnostic when ``--debug`` is on.

    Still stderr-only, and that is load-bearing rather than incidental: the
    hook writes the injected context block to stdout and the client reads
    stdout only, so a line landing there is swallowed into the user's prompt.
    structlog satisfies this from both directions — a configured process routes
    through the handler that ``logging.StreamHandler(sys.stderr)`` pins, and an
    unconfigured one (the usual case for a hook, which never calls
    ``configure_observability``) hits the stderr fallback installed by
    ``witan_core.observability.logging``. Do not "simplify" either end.

    The ``enabled`` flag stays rather than deferring to the log level: the flag
    is the documented ``--debug`` contract, and routing it through level
    configuration would make a stray ``WITAN_LOG_LEVEL=DEBUG`` in someone's
    environment start writing to a hook that is supposed to be silent.
    """
    if enabled:
        logger.debug("witan.context.debug", detail=msg)


def _dbg_exc(enabled: bool, msg: str) -> None:
    """:func:`_dbg` plus the active exception's traceback.

    Replaces a ``_dbg(...)`` / ``traceback.print_exc(file=sys.stderr)`` pair.
    One record carrying ``exc_info`` keeps the message and its traceback
    together — printing the traceback separately meant the two could interleave
    with anything else on stderr, and in JSON mode the traceback would not be
    part of the event at all.
    """
    if enabled:
        # ruff's LOG014 only sees that this body is not lexically inside an
        # `except`. Every caller invokes it from one, and `exc_info=True`
        # resolves through `sys.exc_info()` at call time, so the active
        # exception is captured correctly. Inlining the call at each of the
        # three call sites to satisfy the lint would just duplicate it.
        logger.debug(
            "witan.context.debug",
            detail=msg,
            exc_info=True,  # noqa: LOG014
        )


def inject_context(
    graph_uri: str,
    queries_dir: Path,
    token: str | None,
    debug: bool = False,
    graph_id: str | None = None,
    author: str | None = None,
) -> str:
    """Return markdown context for active projects + ready tasks, or empty string.

    Builds the same output as ``workflow-context-inject.sh`` without the
    checkout-relative QUERIES_DIR assumption.

    ``author`` is the local identity (``cfg.author``) tasks are matched against
    to find the ones this machine holds, so their unread comments can be
    surfaced. Omitted, that block is simply skipped — the rest is unaffected.

    With ``debug=True`` the fault-tolerant swallows still return ``""`` (the hook
    must never crash a prompt), but each one first reports *why* to stderr — a
    broken graph, missing git, or empty result is otherwise indistinguishable
    from "nothing to show".
    """
    try:
        # Kept inside the try so the hook still degrades to "" on the off chance
        # any of this raises — the module contract is that it never does.
        repo, branch = _cached_repo_and_branch()
        _dbg(debug, f"detected repo={repo!r} branch={branch!r}")
        _dbg(debug, f"graph_uri={graph_uri!r} output_cache_ttl={_output_cache_ttl()}s")

        # Serve a recently-rendered block without touching the graph at all.
        cached = _read_output_cache(graph_uri, repo, branch)
        if cached is not None:
            _dbg(debug, f"served from output cache ({len(cached)} chars)")
            return cached

        client = OmnigraphClient(graph_uri, queries_dir, token, graph_id=graph_id)

        # The "list_unscoped_tasks" query is an all-tasks scan (capped at the
        # query's own limit 10000). Derive both the unscoped and the repo-scoped
        # sets from that one result rather than issuing a second
        # list_tasks_by_repo read — each omnigraph read is fixed overhead
        # regardless of row count, so fewer reads is the win, not narrower results.
        all_rows = client.read("read.gq", "list_unscoped_tasks", {})
        unscoped = [r for r in all_rows if not r.get("repo")]

        projects: list[dict] = []
        repo_tasks: list[dict] = []
        stale_repo_case = False
        if repo:
            projects_active = client.read(
                "read.gq",
                "list_projects_by_status",
                {"status": "active"},
            )
            projects = [p for p in projects_active if repo in (p.get("repos") or [])]
            repo_tasks = [r for r in all_rows if r.get("repo") == repo]
            # Cheap nudge (issue #142): reuses the reads already done above —
            # no extra graph round trip. A row whose repo case-insensitively
            # matches the (now-canonical) detected repo but isn't identical is
            # pre-migration data that `list ... == repo` silently drops.
            stale_repo_case = _stale_repo_case_present(repo, all_rows, projects_active)

        # Unscoped (no repo) and repo-scoped sets are disjoint; the dedup is just
        # belt-and-suspenders.
        seen = {t["slug"] for t in repo_tasks}
        tasks = repo_tasks + [t for t in unscoped if t["slug"] not in seen]
        _dbg(
            debug,
            f"graph reads OK: projects={len(projects)} "
            f"repo_tasks={len(repo_tasks)} unscoped={len(unscoped)}",
        )
    except Exception:  # noqa: BLE001
        _dbg_exc(debug, "FAILED building context (returning empty block)")
        return ""

    # Isolated (like the CodeBranch read below): one read grouped by project
    # replaces one read per shown project, but a failing/absent sessions query
    # must still only drop the resume/staleness lines, never blank the
    # projects/ready-tasks context. Sessions with no project_slug are skipped.
    sessions_by_project: dict[str, list[dict]] = {}
    if projects:
        try:
            for s in client.read("read.gq", "list_all_sessions", {}):
                p_slug = s.get("project_slug")
                # Skip retry-minted duplicates so the per-phase staleness count
                # ("18 sessions in implementation") reflects real working stints.
                if p_slug and not s.get("superseded_by"):
                    sessions_by_project.setdefault(p_slug, []).append(s)
        except Exception:  # noqa: BLE001
            _dbg_exc(debug, "sessions read failed (skipping resume/staleness lines)")
            sessions_by_project = {}

    # Isolated from the block above: a CodeBranch query failing (e.g. an
    # existing store that hasn't run `witan migrate schema` since CodeBranch
    # was added) must never blank the projects/ready-tasks context that
    # already works.
    branch_tasks: list[dict] = []
    if repo and branch:
        try:
            branch_tasks = client.read(
                "read.gq",
                "code_branch_tasks",
                {"branch_slug": f"{repo}|{branch}"},
            )
        except Exception:  # noqa: BLE001
            _dbg_exc(
                debug, "code_branch_tasks read failed (run `witan migrate schema`?)"
            )
            branch_tasks = []
    open_branch_tasks = [t for t in branch_tasks if t.get("status") != "closed"]

    # Isolated for the same reason as the CodeBranch read above: a store that
    # has not re-applied ``schema.pg`` since ``TaskComment`` was added answers
    # `unknown node type`, and that must cost this block only.
    def comments_for(slug: str) -> list[dict]:
        return client.read("read.gq", "list_task_comments", {"task_slug": slug})

    commented = _held_task_comments(
        _held_by(all_rows, author), comments_for, graph_uri, debug=debug
    )

    # Shared with ``task_ready`` so the injected list and the tool agree —
    # including the reclaim of ``in_progress`` tasks whose lease has lapsed.
    ready = readiness.filter_ready(tasks)
    _dbg(
        debug,
        f"ready={len(ready)} open_branch_tasks={len(open_branch_tasks)} "
        f"sessions_for={len(sessions_by_project)} project(s)",
    )

    return _render_context_block(
        cache_key=graph_uri,
        repo=repo,
        branch=branch,
        projects=projects,
        sessions_by_project=sessions_by_project,
        ready=ready,
        open_branch_tasks=open_branch_tasks,
        commented=commented,
        stale_repo_case=stale_repo_case,
        debug=debug,
    )


# A prompt preamble has to stay readable, and someone holding four tasks at once
# has a bigger problem than an elided comment. All three caps bound the block;
# the full thread is always one ``task_get`` away, which the block says.
#
# ``_COMMENT_RENDER_LIMIT`` is the one that makes "bounded" true rather than
# merely likely: comments are append-only and unread ones accumulate, so without
# a count cap a flooded task injects an arbitrarily large prompt no matter how
# short each preview is. Comments past it stay undelivered — not dropped — and
# lead the next block.
_HELD_TASK_LIMIT = 3
_COMMENT_RENDER_LIMIT = 5
_COMMENT_PREVIEW_CHARS = 400


def _held_by(rows: list[dict], author: str | None) -> list[dict]:
    """The ``in_progress`` tasks among ``rows`` that ``author`` holds.

    Person-level, not session-level: a comment on a task another of your own
    sessions claimed is still addressed to you, and this session is as able to
    act on it as that one.

    Lease expiry is deliberately NOT consulted. It answers "may someone else
    take this?", which is a different question from "is this the work in front
    of me?" — and the 60-minute lease lapses routinely mid-task, exactly when a
    correction is most worth reading.
    """
    if not author:
        return []
    return [
        r
        for r in rows
        if r.get("status") == "in_progress"
        and readiness.holder_identity(r.get("assignee")) == author
    ]


def _held_task_comments(
    held: list[dict],
    comments_for: Callable[[str], list[dict]],
    cache_key: str,
    *,
    debug: bool,
) -> list[tuple[dict, list[dict]]]:
    """``(task, unread comments)`` for each held task that has any.

    ``comments_for`` is how the caller's transport fetches one task's thread —
    a direct graph read locally, ``task_get`` over MCP against a deployment.
    Nothing here is fetched unless a task is actually held, so the common case
    (holding nothing) costs no reads at all.

    Unread is decided against the local delivered-slug record, which is only
    added to by :func:`_render_context_block` — so a comment that was fetched
    but never rendered (the block was dropped, the caller failed later, it fell
    past ``_COMMENT_RENDER_LIMIT``) is still unread next time.
    """
    if not held:
        return []
    seen = _read_seen(cache_key)
    out: list[tuple[dict, list[dict]]] = []
    for task in held[:_HELD_TASK_LIMIT]:
        try:
            comments = comments_for(task["slug"])
        except Exception:  # noqa: BLE001 — never blank the rest of the block
            _dbg_exc(debug, f"comment read failed for {task['slug']!r}")
            continue
        delivered = set(seen.get(task["slug"]) or ())
        unread = [c for c in comments if c.get("slug") not in delivered]
        if unread:
            out.append((task, unread))
    _dbg(debug, f"held={len(held)} task(s) with unread comments={len(out)}")
    return out


def _render_context_block(
    *,
    cache_key: str,
    repo: str | None,
    branch: str | None,
    projects: list[dict],
    sessions_by_project: dict[str, list[dict]],
    ready: list[dict],
    open_branch_tasks: list[dict],
    commented: list[tuple[dict, list[dict]]],
    stale_repo_case: bool,
    debug: bool,
) -> str:
    """Render the markdown block both the local and remote data paths share.

    ``cache_key`` stands in for ``graph_uri`` in the output-cache key — for the
    remote path (:func:`inject_context_remote`) that is the deployment's own
    URL rather than a store path, so a laptop's local graph and a deployment
    never collide on the same cache file (see ``_output_cache_file``). It keys
    the delivered-comment record for the same reason.

    Recording delivery is this function's job rather than the callers' because
    rendering IS the delivery: the string returned here is injected into the
    prompt, so a comment that reaches this point has been shown — and only the
    ones that reach it, which is why the ``_COMMENT_RENDER_LIMIT`` slice and the
    record are built from the same list.
    """
    lines: list[str] = []
    # What this render actually delivers, which is what gets recorded below.
    rendered: list[tuple[dict, list[dict]]] = []

    # First, ahead of the projects and ready-work blocks. A comment on work you
    # are already holding is the only thing here addressed to you specifically,
    # and it is usually a correction to the plan you are mid-way through.
    if commented:
        lines += [
            "## ⚠ New Comments on Work You Hold",
            "",
            "Another agent or teammate has commented on a task you have "
            "claimed. Read this before continuing — a comment is how someone "
            "says the task's own description is wrong without overwriting it:",
            "",
        ]
        for task, unread in commented:
            shown = unread[:_COMMENT_RENDER_LIMIT]
            rendered.append((task, shown))
            lines.append(
                f"- **{task.get('title', task['slug'])}** (slug: `{task['slug']}`)"
            )
            for comment in shown:
                body = " ".join((comment.get("body") or "").split())
                if len(body) > _COMMENT_PREVIEW_CHARS:
                    body = body[:_COMMENT_PREVIEW_CHARS].rstrip() + "…"
                lines.append(f"  - {comment.get('author', 'unknown')}: {body}")
            if (held_back := len(unread) - len(shown)) > 0:
                lines.append(
                    f"  - …and {held_back} more unread — still undelivered, "
                    "they lead the next block."
                )
        lines += [
            "",
            "Full threads (and any older comments elided here) are in "
            "`task_get(slug=...)`. Reply with `task_comment(slug=..., text=...)`.",
            "",
        ]

    if projects:
        proj_header = f"This repository has {len(projects)} active tracked project(s)"
        if len(projects) > 3:
            proj_header += " — showing the first 3, run `witan projects` for all"
        lines += [
            "## Active Workflow Projects",
            "",
            f"{proj_header}:",
            "",
        ]
        for p in projects[:3]:
            lines.append(f"- **{p['title']}** (slug: `{p['slug']}`)")
            lines.append(f"  Phase: {p['phase']}")
            if p.get("github_issue"):
                lines.append(f"  Issue: {p['github_issue']}")
            lines.extend(
                _project_session_lines(sessions_by_project.get(p["slug"], []), p)
            )
        lines += [
            "",
            "If this session is contributing to one of the projects above, call",
            "`workflow_session_start` with the matching slug and the current phase",
            "before doing substantive work.",
            "",
        ]

    if open_branch_tasks:
        lines += [
            "## In-Flight Branch",
            "",
            "The current git branch is already linked to task(s) in progress:",
            "",
        ]
        for t in open_branch_tasks:
            held_by = f" (claimed by {t['assignee']})" if t.get("assignee") else ""
            lines.append(f"- **{t['title']}** (slug: `{t['slug']}`){held_by}")
        lines += [
            "",
            "This is likely the work this session should continue, not a new task.",
            "",
        ]

    if ready:
        ready_header = f"{len(ready)} task(s) are ready to work (no open blockers)"
        if len(ready) > 5:
            ready_header += (
                " — showing the top 5 by priority, run `witan tasks` for all"
            )
        lines += [
            "## Ready Tasks",
            "",
            f"{ready_header}:",
            "",
        ]
        for t in ready[:5]:
            ext = f" · {t['external_uri']}" if t.get("external_uri") else ""
            lines.append(
                f"- `[{t.get('priority', 'p2')}]` **{t['title']}**"
                f" (slug: `{t['slug']}`){ext}"
            )
        lines += [
            "",
            "Claim one with `task_claim(slug=...)` BEFORE working it — an "
            "unclaimed task in progress is indistinguishable from an idle one, "
            "and a second session will start the same work. Then `task_close` "
            "it when done (or `/witan-task`).",
            "",
        ]

    if stale_repo_case:
        lines += [
            "## ⚠ Unmigrated Repo Keys",
            "",
            "This store has task/project records for this repo under a "
            "different letter case — a data-fragmentation bug (issue #142) "
            "whose fix needs a one-time backfill. Run `witan migrate "
            "repo-keys` once; until then, reads scoped to this repo "
            "(`task_ready`, `memory_list`, ...) may be missing results.",
            "",
        ]

    if not lines:
        output = ""
    else:
        if projects:
            lines.append("If this is unrelated work, ignore the above.")
        output = "\n".join(lines)

    if commented:
        seen = _read_seen(cache_key)
        for task, shown in rendered:
            already = seen.get(task["slug"]) or []
            seen[task["slug"]] = already + [
                s for c in shown if (s := c.get("slug")) and s not in already
            ]
        _write_seen(cache_key, seen)

    # Cache the freshly rendered block (including an empty one) so the next
    # prompt in this window skips the graph reads entirely — EXCEPT when it
    # carries comments. Delivery was just recorded, so re-serving this string
    # for the rest of the TTL would re-interrupt with comments the record now
    # calls delivered, turning "interrupts once" into "interrupts for 30
    # seconds". Skipping the write costs one extra round of graph reads on the
    # next prompt, which then renders without the block and caches that.
    if commented:
        _dbg(debug, "not caching a render carrying comments (one-time delivery)")
    else:
        _write_output_cache(cache_key, repo, branch, output)
    _dbg(
        debug,
        f"rendered {len(output)} chars"
        + ("" if output else " (empty — no projects, ready tasks, or branch tasks)"),
    )
    return output


def inject_context_remote(server, remote_url: str, debug: bool = False) -> str:
    """Same rendered block as :func:`inject_context`, sourced from the
    deployment's own MCP tool surface instead of a direct :class:`OmnigraphClient`
    read.

    A ``remote_url``-only deployment target has no direct graph endpoint the
    CLI is allowed to open — ``inject_context`` calling ``OmnigraphClient``
    unconditionally is exactly the bug this function fixes (agent-kit#261's
    mechanism, still live on this path: see
    tk-witan-hook-context-reads-the-local-store-on-a-de-dfb2c9). ``server`` is
    a tool-calling proxy built the same way ``_srv()`` builds one for a remote
    target (``witan.cli._common.remote_proxy``), so this makes exactly the
    tool calls an agent's own session would make.

    Held-task identity is the one thing this path does BETTER: the local one
    matches against ``cfg.author``, while ``task_list(assignee="@me")`` is
    resolved from the caller's token by the deployment — the only identity that
    is correct there, since the two namespaces need not agree (see
    ``server.claim_authorship``).

    Degrades relative to :func:`inject_context` in one remaining place:
    stale-repo-case detection has no remote-tool equivalent and is always
    False here — recorded via ``--debug``, never silently. Branch-linked task
    association (the ``## In-Flight Branch`` section) no longer degrades; it
    goes through ``task_for_branch``
    (tk-the-in-flight-branch-anti-duplicate-work-signal--073f96), because on a
    shared graph that block is the signal keeping two actors off the same
    branch — the case where dropping it costs the most, not the least.
    """
    try:
        repo, branch = _cached_repo_and_branch()
        _dbg(debug, f"detected repo={repo!r} branch={branch!r} (remote)")
        _dbg(
            debug, f"remote_url={remote_url!r} output_cache_ttl={_output_cache_ttl()}s"
        )

        cached = _read_output_cache(remote_url, repo, branch)
        if cached is not None:
            _dbg(debug, f"served from output cache ({len(cached)} chars)")
            return cached

        # Mirrors the local path's gating: no detected repo means no project
        # list (workflow_project_list(repo="") would return every repo's
        # active projects, not "none" — see its docstring), but ready tasks
        # still fall through to the unscoped set either way (task_ready's own
        # repo_module.detect(override="") resolves to "no repo", matching
        # list_unscoped_tasks locally). limit=10000 matches the local path's
        # list_unscoped_tasks cap so a busy graph's header count isn't
        # silently truncated by task_ready's own default limit=20.
        projects = (
            server.workflow_project_list(repo=repo, status="active") if repo else []
        )
        ready = server.task_ready(repo=(repo or ""), limit=10000)
        _dbg(debug, f"remote reads OK: projects={len(projects)} ready={len(ready)}")
    except Exception:  # noqa: BLE001
        _dbg_exc(debug, "FAILED building remote context (returning empty block)")
        return ""

    sessions_by_project: dict[str, list[dict]] = {}
    for p in projects[:3]:
        try:
            sessions_by_project[p["slug"]] = server.workflow_session_list(
                project_slug=p["slug"]
            )
        except Exception:  # noqa: BLE001
            _dbg_exc(
                debug, f"sessions read failed for {p['slug']!r} (skipping resume lines)"
            )

    # Isolated exactly as the local path isolates its own CodeBranch read
    # (see :func:`inject_context`): a deployment that predates
    # ``task_for_branch`` answers with an unknown-tool error, and that must
    # cost the branch block only — never the projects/ready-tasks block that
    # already rendered above it.
    open_branch_tasks: list[dict] = []
    if repo and branch:
        try:
            open_branch_tasks = [
                t
                for t in server.task_for_branch(branch=branch, repo=repo)
                if t.get("status") != "closed"
            ]
        except Exception:  # noqa: BLE001
            _dbg_exc(
                debug,
                "task_for_branch failed (deployment may predate it) — "
                "no In-Flight Branch block",
            )

    _dbg(
        debug,
        f"remote branch tasks: open={len(open_branch_tasks)}; "
        "stale-repo-case detection unavailable (no remote tool equivalent yet)",
    )

    # Isolated like the branch read above: a deployment predating `@me` or
    # `TaskComment` costs this block and nothing else.
    #
    # `repo=""` is load-bearing, not decoration. An OMITTED repo is filled in
    # client-side by `RemoteMCPProxy._map_args` (`task_list` is in
    # `_REPO_IS_SCOPE_OR_STAMP`), which would scope this to whichever checkout
    # the hook fired in — while the local path answers from an all-repos
    # `list_unscoped_tasks` scan. The sentinel makes both transports answer the
    # same question: which tasks do I hold, anywhere.
    try:
        held = server.task_list(assignee="@me", status="in_progress", repo="")
    except Exception:  # noqa: BLE001
        _dbg_exc(debug, "held-task read failed — no comment block")
        held = []

    def comments_for(slug: str) -> list[dict]:
        return (server.task_get(slug=slug) or {}).get("comments") or []

    commented = _held_task_comments(held, comments_for, remote_url, debug=debug)

    return _render_context_block(
        cache_key=remote_url,
        repo=repo,
        branch=branch,
        projects=projects,
        sessions_by_project=sessions_by_project,
        ready=ready,
        open_branch_tasks=open_branch_tasks,
        commented=commented,
        stale_repo_case=False,
        debug=debug,
    )


# A project sitting through this many sessions in the same phase without
# advancing is a soft signal that it may be stuck — nudge, don't block.
_STALE_SESSION_THRESHOLD = 4


def _project_session_lines(sessions: list[dict], project: dict) -> list[str]:
    """Continuity + staleness lines for one project from its sessions.

    ``sessions`` is this project's slice of the single all-sessions read, ordered
    by ``started_at`` asc. Returns up to two lines: the latest session's handoff
    summary (the artifact written by ``workflow_session_end`` that is otherwise
    invisible on resume) and a staleness nudge when many sessions have accrued in
    the current phase without advancing. Pure and fault-tolerant: an empty list
    yields ``[]`` so a missing/failed sessions read can never blank the
    projects/tasks context that already works.
    """
    if not sessions:
        return []

    out: list[str] = []

    latest = sessions[-1]  # ordered by started_at asc → last is newest
    summary_lines = (latest.get("summary") or "").strip().splitlines()
    summary = summary_lines[0][:200] if summary_lines else ""
    if summary:
        state = "still open" if not latest.get("ended_at") else "ended"
        out.append(f"  Last session ({state}): {summary}")

    phase = project.get("phase")
    if phase:
        in_phase = sum(1 for s in sessions if s.get("phase") == phase)
        if in_phase >= _STALE_SESSION_THRESHOLD:
            out.append(
                f"  ⚠ {in_phase} sessions in `{phase}` — if this phase is done, "
                "call `workflow_project_advance` (or `workflow_project_complete`)."
            )
    return out


def _cached_repo_and_branch() -> tuple[str | None, str | None]:
    """``(repo, branch)`` for the current checkout, cached on disk with a short
    TTL so the prompt hook doesn't spawn git on every prompt.

    Fully fault-tolerant: a cache miss, unreadable/stale entry, or any detection
    error falls through to (or returns) live values and never raises. When
    ``WITAN_REPO`` is set, detection needs no git at all, so the cache is skipped.
    """
    project_dir = _cwd_or_dot()

    # WITAN_REPO short-circuits git — nothing to amortize, and it can differ from
    # what a dir-keyed cache holds, so don't consult/write the cache in that mode.
    # Skip branch detection when there's no repo (e.g. WITAN_REPO=""): a branch
    # is only used to join CodeBranch, so it's wasted git work without a repo.
    if os.environ.get("WITAN_REPO") is not None:
        repo = _detect_repo()
        return repo, (_current_branch() if repo else None)

    digest = hashlib.sha1(project_dir.encode()).hexdigest()[:16]
    cache_file = session_state.session_state_dir() / f"witan-repo-{digest}.json"
    try:
        data = json.loads(cache_file.read_text())
        if time.time() - data["stamp"] < _REPO_CACHE_TTL:
            return data.get("repo"), data.get("branch")
    except Exception:  # noqa: BLE001
        # Same as the output cache: absent on the first prompt in a window, so
        # debug rather than warning.
        logger.debug("witan.context.repo_cache_miss", exc_info=True)

    repo = _detect_repo()
    branch = _current_branch() if repo else None
    _atomic_write_private(
        cache_file, json.dumps({"stamp": time.time(), "repo": repo, "branch": branch})
    )
    return repo, branch


def _cwd_or_dot() -> str:
    """``$CLAUDE_PROJECT_DIR`` or the cwd, degrading to ``"."`` — ``Path.cwd()``
    itself raises ``OSError`` if the working directory was deleted."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        return project_dir
    try:
        return str(Path.cwd())
    except OSError:
        return "."


def _current_branch() -> str | None:
    try:
        return repo_module.current_branch()
    except Exception:  # noqa: BLE001
        # Debug: running outside a git checkout is a supported situation, not a
        # malfunction — the branch only joins CodeBranch rows.
        logger.debug("witan.context.branch_detect_failed", exc_info=True)
        return None


def _detect_repo() -> str | None:
    """Detect canonical repo URI from WITAN_REPO, CLAUDE_PROJECT_DIR, or cwd.

    WITAN_REPO="" (explicitly set to empty string) suppresses detection entirely.

    Shares ``repo`` module's resolution (``origin`` first, then the first remote
    of any name) and its normaliser, so the hook and ``repo.detect`` can't drift
    — but keyed off ``CLAUDE_PROJECT_DIR``/cwd rather than ``Path.cwd()`` because
    a persistent MCP server's cwd is not the session's checkout.
    """
    witan_repo = os.environ.get("WITAN_REPO")
    if witan_repo is not None:
        # "" → disabled; non-empty → canonicalized the same way a detected
        # remote is, so this hook's repo can never drift from repo.detect()'s.
        return repo_module.normalise(witan_repo) if witan_repo else None

    project_dir = _cwd_or_dot()
    try:
        raw = repo_module.git_remote_url(Path(project_dir))
    except Exception:  # noqa: BLE001 — the prompt hook must never crash
        # Debug for the same reason as the branch probe: no remote, or no repo
        # at all, is a normal place to run an agent from.
        logger.debug("witan.context.repo_detect_failed", exc_info=True)
        return None
    return repo_module.normalise(raw) if raw else None


# The Stop hook's session auto-close lives in ``witan.cli.hooks`` — it dispatches
# through ``_srv()`` so it reaches the deployment when one is configured, which a
# direct OmnigraphClient write here could not.
