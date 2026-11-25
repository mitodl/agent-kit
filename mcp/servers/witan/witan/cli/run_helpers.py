"""Shared helpers for picking, launching, and prompting agent run sessions."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from witan_core.client_instance import CLIENT_INSTANCE_ENV, new_client_instance
from witan_core.observability import get_logger

from .. import config as config_module
from .. import envelope, provenance
from .. import repo as repo_module
from ..config import Config
from ..harness.transport import agent_environ
from ..prompts import prompts_off
from ..readiness import CLAIM_LEASE_SECONDS
from ..session_state import SESSION_ID_ENV_VARS
from ._common import (
    _fn,
    _srv,
    console,
    esc,
    print_error,
    stderr_console,
)
from .selected_target import selected_target

logger = get_logger("witan.cli.run_helpers")

# The size of a claim's list, for a prompt with no claim to take one from.
_PROMPT_MEMORY_LIMIT = 3
_PROMPT_EXCERPT_CHARS = 280
_PROJECT_PROMPT_TASK_LIMIT = 20

LAUNCH_SPEC_VERSION = 1
"""Bumped when a launch-spec field is removed or changes meaning, not when one
is added (docs/adr/0012, "3. The launch contract")."""

# Variables that override the target matching and repo detection the launched
# agent would otherwise do from its cwd. They outrank `cwd`, so an inherited
# one would point the agent somewhere other than the folder it was started in.
_LOCATION_ENV_VARS = ("CLAUDE_PROJECT_DIR", "WITAN_REPO")

LaunchKind = Literal["task", "project"]


class LaunchSpec(TypedDict):
    """What to run for a launch, for a client that runs it in its own terminal.

    The client starts from the environment it gives any terminal, removes each
    name in ``unset_env``, then sets ``env`` over it.
    """

    version: int
    kind: LaunchKind
    slug: str
    launchable: Literal[True]
    argv: list[str]
    cwd: str
    env: dict[str, str]
    unset_env: list[str]
    handoff: bool
    """Whether the prompt may go to the editor's own chat panel instead. Never
    for a spec that took a claim."""
    prompt: str
    agent: str
    model: str | None
    claim: dict | None
    release: NotRequired[dict]
    """From the language server, for a spec that took a claim: the arguments
    to pass ``witan.releaseTask`` verbatim if the launch fails."""


class LaunchRefusal(TypedDict):
    """Why a launch was refused. A refusal never carries an ``argv``.

    ``reason`` is ``not_found``, ``no_workspace`` (``repos`` names what no open
    folder maps to), ``target_mismatch`` (the launcher used the local store,
    and an agent started in the folder would match ``target`` instead), or the
    refused claim's own ``reason``, with the claim result itself in ``claim``.
    """

    version: int
    kind: LaunchKind
    slug: str
    launchable: Literal[False]
    reason: str
    message: str
    claim: dict | None
    repos: NotRequired[list[str]]
    target: NotRequired[str]


def _parse_number_selection(raw: str, count: int) -> list[int]:
    """Parse "1 3 5", "1-3", "2,4", or "all" into 0-based indices."""
    raw = raw.strip()
    if not raw:
        return []
    if raw.lower() == "all":
        return list(range(count))
    indices: set[int] = set()
    for part in raw.replace(",", " ").split():
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                indices.update(range(int(lo) - 1, int(hi)))
            except ValueError:
                pass
        else:
            try:
                indices.add(int(part) - 1)
            except ValueError:
                pass
    return sorted(i for i in indices if 0 <= i < count)


def _pick_items(items: list[dict], render_fn) -> list[dict]:
    """Show a numbered list and return user-selected items."""
    for i, item in enumerate(items, 1):
        console.print(f"  [bold]{i:2d}.[/bold] {render_fn(item)}")
    console.print()
    try:
        raw = input('Select (e.g. "1 3", "1-3", "all", or Enter for none): ').strip()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Selection cancelled.[/yellow]")
        raise SystemExit(0)
    if not raw:
        return []
    indices = _parse_number_selection(raw, len(items))
    return [items[i] for i in indices]


def _agent_argv(agent: str, model: str | None, prompt: str) -> list[str]:
    return [agent, *(["--model", model] if model else []), prompt]


def _print_prompt(prompt: str) -> None:
    # Not esc(): a dry run exists to show the exact text the agent will
    # receive, so it must not be shown with escapes in it. All three of
    # Rich's substitutions are off — `emoji=False` because a prompt saying
    # `:warning:` renders as ⚠ while the agent receives the literal
    # characters, which is the same class of lie the escaping fixes.
    console.print(prompt, markup=False, emoji=False, highlight=False)


def _launch_agent(
    cfg,
    resolved_agent: str,
    resolved_model: str | None,
    prompt: str,
    dry_run: bool,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    if dry_run:
        _print_prompt(prompt)
        return
    target_info = f" [{cfg.target_name}]" if cfg.target_name else ""
    model_info = f" --model {resolved_model}" if resolved_model else ""
    console.print(f"[dim]Launching: {resolved_agent}{model_info}{target_info}[/dim]")
    try:
        subprocess.run(
            _agent_argv(resolved_agent, resolved_model, prompt),
            check=False,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError:
        print_error(f"Agent {resolved_agent!r} not found on PATH.")
        raise SystemExit(1) from None


def _prompt_memories(s, slug: str, claim: dict | None = None) -> list[dict]:
    """Memories for a task's prompt, in the shape ``task_claim`` returns them.

    A first claim already chose them, and using its list is what makes the
    ``witan.memory.surfaced`` line it logged describe what this session got. A
    renewal, ``--no-claim`` and ``--dry-run`` have no list, so those look it up.

    A failed lookup must not stop the agent launching: it is the task the agent
    came for, and ``recall`` can still find these later.

    :param s: The server or remote proxy.
    :param slug: The task's slug.
    :param claim: The ``task_claim`` result, when this run made one.
    :returns: Memories with ``slug``, ``title``, ``author`` and ``excerpt``.
    :rtype: list[dict]
    """
    if claim is not None and "memories" in claim:
        return claim["memories"]
    try:
        result = _fn(s.recall)(task=slug, hops=0, limit=_PROMPT_MEMORY_LIMIT)
    except Exception:  # never block the launch
        # Logged, not printed: a launch spec's caller may own stdout (the
        # language server's JSON-RPC channel, or `--emit-spec`'s JSON).
        logger.warning("witan.launch.memories_skipped", slug=slug, exc_info=True)
        return []
    return [
        {**m, "excerpt": m["content"][:_PROMPT_EXCERPT_CHARS]}
        for m in result["memories"]
    ]


def _project_prompt_memories(s, slug: str, tasks: list[dict]) -> list[dict]:
    """Memories for a project's prompt, matched to its ready tasks server-side.

    A failed lookup must not stop the agent launching, for the reason
    :func:`_prompt_memories` gives.

    :param s: The server or remote proxy.
    :param slug: The project's slug.
    :param tasks: The ready tasks the prompt lists, in its order.
    :returns: Memories with ``slug``, ``title``, ``author``, ``excerpt`` and
        the ``task`` each matched.
    :rtype: list[dict]
    """
    try:
        return _fn(s.workflow_project_run_memories)(
            slug=slug, task_slugs=[t["slug"] for t in tasks]
        )
    except Exception:  # never block the launch
        logger.warning("witan.launch.memories_skipped", slug=slug, exc_info=True)
        return []


def _memory_lines(memories: list[dict], subject: str = "this task") -> list[str]:
    """The prompt section for ``memories``, or nothing when there are none.

    Attributed and labelled as notes, and each collapsed to one line, because
    anyone on the team can write a memory and this reaches an agent's first
    turn, where a line of text would otherwise read as part of the assignment.
    A memory carrying the ``task`` it matched names it.
    """
    if not memories:
        return []
    lines = [
        "",
        (
            f"Team memories that match {subject}. {provenance.TEAMMATE_NOTES} "
            "The full text of each is in memory_get:"
        ),
    ]
    for m in memories:
        author, title, excerpt = (
            " ".join(m[field].split()) for field in ("author", "title", "excerpt")
        )
        matched = f", matches {m['task']}" if m.get("task") else ""
        lines += [f"  {m['slug']} ({author}{matched}): {title}", f"    {excerpt}"]
        # The recall fallback's memories carry no `screening` (recall is not
        # labelled yet), and a clean verdict has no notice.
        if notice := m.get("screening", {}).get("notice"):
            lines.append(f"    {notice}")
    return lines


def _overlap_lines(overlaps: list[dict]) -> list[str]:
    """The in-flight tasks a claim found on the same code, or nothing.

    Each title is collapsed to one line for the reason ``_memory_lines`` gives:
    anyone can write one, and it reaches the claimer's terminal or prompt.
    """
    if not overlaps:
        return []
    lines = [
        "",
        (
            "In-flight tasks on the same code. Read each before editing shared "
            "code, and task_comment it if the plans collide:"
        ),
    ]
    for o in overlaps:
        title = " ".join(o["title"].split())
        shared = (
            f"shared symbols: {', '.join(o['symbols'])}"
            if o["symbols"]
            else f"shared files: {', '.join(o['files'])}"
        )
        lines += [f"  {o['slug']} ({o['assignee']}): {title}", f"    {shared}"]
    return lines


def _run_prompt(
    t: dict,
    *,
    claimed: bool,
    memories: list[dict] | None = None,
    overlaps: list[dict] | None = None,
) -> str:
    lines = [
        "Work on this task from the witan task graph and see it through to completion.",
        "",
        f"Task:     {t['slug']}",
        f"Title:    {t.get('title', '')}",
        f"Type:     {t.get('type')}    Priority: {t.get('priority')}    Status: {t.get('status')}",
    ]
    for label, key in (
        ("Repo", "repo"),
        ("Project", "project_slug"),
        ("Reference", "external_uri"),
    ):
        if t.get(key):
            lines.append(f"{label}:{' ' * (10 - len(label))}{t[key]}")
    if t.get("symbol_refs"):
        lines.append(
            f"Symbols:  {', '.join(t['symbol_refs'])}  (use the code_* tools to inspect)"
        )
    # Say which it is rather than instructing unconditionally: `witan task run`
    # claims before launching, so telling that agent to claim again would have
    # it re-take a lease it already holds, and telling it nothing would leave a
    # `--no-claim` agent working a task that still reads as unclaimed.
    #
    # The claimed branch names the lease window rather than promising exclusion
    # outright: the claim is advisory and lapses after CLAIM_LEASE_SECONDS with
    # nothing on the launch path renewing it, so a task worked for longer than
    # that reappears in `task_ready` under the agent's feet. Re-calling
    # `task_claim` renews it, which the agent can only do if it is told to.
    lease_minutes = CLAIM_LEASE_SECONDS // 60
    held = (
        f"This task is claimed for you. The claim is a lease, not a lock: it "
        f"lapses after {lease_minutes} minutes and nothing renews it for you, "
        f"so if the work runs longer, call "
        f'task_claim(slug="{t["slug"]}", session_id="<your agent session id>") '
        f"again to renew — otherwise the task returns to ready work while you "
        f"are still on it. The claim was taken before your session existed, so "
        f"it may name no session; the renewal records yours."
        if claimed
        else f'This task is NOT claimed. Claim it before you start: task_claim(slug="{t["slug"]}").'
    )
    lines += [
        "",
        "Description:",
        t.get("description") or "(none)",
        *_memory_lines(memories or []),
        *_overlap_lines(overlaps or []),
        "",
        held,
        (
            f'When complete, close it: task_close(slug="{t["slug"]}", resolution="<what you did>"). '
            f'File any follow-up work with task_create(discovered_from=["{t["slug"]}"], ...).'
        ),
    ]
    return "\n".join(lines)


def _project_run_prompt(
    p: dict, tasks: list[dict], memories: list[dict] | None = None
) -> str:
    lines = [
        "Work on this workflow project and advance it through its current phase.",
        "",
        f"Project:  {p['slug']}",
        f"Title:    {p.get('title', '')}",
        f"Phase:    {p.get('phase')}    Status: {p.get('status')}",
    ]
    repos = p.get("repos") or []
    if repos:
        lines.append(f"Repos:    {', '.join(repos)}")
    if p.get("github_issue"):
        lines.append(f"Issue:    {p['github_issue']}")
    lines += [
        "",
        "Description:",
        p.get("description") or "(none)",
    ]
    if p.get("preauthorized"):
        how = ", do these without asking" if envelope.owner_granted(p) else ""
        lines += [
            "",
            f"{envelope.describe(p, 'preauthorized')}{how}:",
            *(f"  - {item}" for item in p["preauthorized"]),
        ]
    if p.get("reserved_decisions"):
        lines += [
            "",
            (
                f"{envelope.describe(p, 'reserved_decisions')}. Don't act on "
                "these; collect them into one numbered list with a "
                "recommendation each, at the next natural stop:"
            ),
            *(f"  - {item}" for item in p["reserved_decisions"]),
        ]
    if tasks:
        lines += ["", "Ready tasks:"]
        for t in tasks:
            pri = t.get("priority", "")
            lines.append(
                f"  {t['slug']}  [{pri}] {t.get('title', '')}"
                + (
                    f"\n    {t.get('description', '')[:120]}"
                    if t.get("description")
                    else ""
                )
            )
    lines += _memory_lines(memories or [], "these tasks")
    lines += [
        "",
        # ★ THE CLAIM LINE IS WHY THIS BLOCK EXISTS.
        # `witan task run` claims before it launches, because it knows which
        # single task the agent is getting. This path cannot: it hands over up
        # to 20 ready tasks and the agent may work one of them or four, so
        # claiming them all here would hold leases on work nobody starts. That
        # left the whole list unclaimed — two sessions picked the same task off
        # it on the same day and wrote the same fix twice, each unaware of the
        # other, because an unclaimed task looks identical to nobody-is-on-it.
        (
            "Claim each task BEFORE you start it, not after: "
            'task_claim(slug="tk-..."). It refuses if another session already '
            "holds it, which is the signal to pick a different one — that refusal "
            "is the only thing standing between you and duplicating someone "
            "else's work."
        ),
        (
            f"A claim is a lease, not a lock: it lapses after "
            f"{CLAIM_LEASE_SECONDS // 60} minutes and nothing renews it for you, "
            f"so call "
            f'task_claim(slug="tk-...", session_id="<your agent session id>") '
            f"again to renew anything you are still working past that."
        ),
        'When a task is done: task_close(slug="tk-...", resolution="<what you did>").',
        'If you claim a task and then decide not to work it: task_release(slug="tk-...").',
        f'When the project phase is complete: workflow_project_advance(slug="{p["slug"]}", summary="<what was accomplished>").',
    ]
    return "\n".join(lines)


@contextmanager
def launched_client_instance(instance: str) -> Iterator[None]:
    """Make calls inside on behalf of the agent about to launch with ``instance``.

    The proxy reads :data:`CLIENT_INSTANCE_ENV` for each call's
    ``X-Witan-Client-Instance`` header, and a local server reads it for its
    own id, so a claim or memory lookup made here is logged under the id the
    launched agent's reads will carry. Restored on exit for the reason
    :func:`bare_session_env` gives.
    """
    saved = os.environ.get(CLIENT_INSTANCE_ENV)
    os.environ[CLIENT_INSTANCE_ENV] = instance
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(CLIENT_INSTANCE_ENV, None)
        else:
            os.environ[CLIENT_INSTANCE_ENV] = saved


@contextmanager
def bare_session_env() -> Iterator[None]:
    """Hide every agent-session id variable from this process while inside.

    A launcher is not an agent session. An id it inherited (a terminal or
    editor started inside one) would qualify the launch claim with that outer
    session, and the launched agent's renewal would be refused as someone
    else's. Bare, the claim is the launched agent's to renew (docs/adr/0012,
    "A launch claim is always bare").

    The values come back on exit because the language server is long-lived:
    every later call it makes still needs whatever session id it started with.
    """
    saved = {
        name: os.environ.pop(name) for name in SESSION_ID_ENV_VARS if name in os.environ
    }
    try:
        yield
    finally:
        os.environ.update(saved)


def launch_env(cfg: Config) -> tuple[dict[str, str], list[str]]:
    """A launch's ``env`` and ``unset_env``: what the agent's environment changes.

    :param cfg: The config the launcher dispatched with.
    :returns: The variables to set, and the names to remove before setting them.
    :rtype: tuple[dict[str, str], list[str]]
    """
    # An inherited client instance names the launcher's own agent, if any; the
    # launched agent is a different client.
    unset_env = [*SESSION_ID_ENV_VARS, *_LOCATION_ENV_VARS, CLIENT_INSTANCE_ENV]
    env: dict[str, str] = {}
    # Pinned so the agent talks to the graph the claim was taken in. The local
    # store has no target name, and the agent's own matching from `cwd`
    # reaches it once an inherited WITAN_TARGET is gone.
    if cfg.target_name:
        env["WITAN_TARGET"] = cfg.target_name
    else:
        unset_env.append("WITAN_TARGET")
    return env, unset_env


def _launch_folder(
    workspace_folders: Sequence[Path], repos: Sequence[str]
) -> Path | None:
    """The open folder a launch runs in, or ``None`` when none fits.

    The invoking folder (the first) when it maps to one of ``repos`` or there
    are none, else the first other folder that maps to one. ``None`` also when
    no folder is open at all.
    """
    if not repos:
        return workspace_folders[0] if workspace_folders else None
    return next(
        (f for f in workspace_folders if repo_module.detect_at(f) in repos), None
    )


def _folder_checkout(folder: Path) -> dict[str, str | None]:
    """``task_claim``'s ``repo`` and ``branch`` for the folder a launch runs in.

    Left out, the claim links the launcher's own checkout, which is a different
    one whenever the launch picked another open folder. ``branch=""`` turns
    the link off when the folder has no repo or no branch (a detached HEAD),
    rather than letting the server fall back to the launcher's.
    """
    repo = repo_module.detect_at(folder)
    branch = repo_module.current_branch(folder) if repo else None
    return {"repo": repo, "branch": branch or ""}


def _handoff_allowed(
    workspace_folders: Sequence[Path], started_with_target: bool
) -> bool:
    """Whether a chat-panel agent, which gets no env, resolves what the spec does.

    It resolves its target and repo from the editor's environment, which this
    process shares, so that has to carry no override and leave one folder to
    resolve from (docs/adr/0012, "5. Launch modes and per-editor fallbacks").
    """
    return (
        len(workspace_folders) == 1
        and not started_with_target
        and not any(
            name in os.environ for name in ("WITAN_TARGET", *_LOCATION_ENV_VARS)
        )
    )


def _refusal(
    kind: LaunchKind,
    slug: str,
    reason: str,
    message: str,
    claim: dict | None = None,
) -> LaunchRefusal:
    return LaunchRefusal(
        version=LAUNCH_SPEC_VERSION,
        kind=kind,
        slug=slug,
        launchable=False,
        reason=reason,
        message=message,
        claim=claim,
    )


def build_launch_spec(
    kind: LaunchKind,
    slug: str,
    *,
    server,
    cfg: Config,
    workspace_folders: Sequence[Path],
    started_with_target: bool,
    agent: str | None = None,
    model: str | None = None,
    claim: bool = True,
    force: bool = False,
) -> LaunchSpec | LaunchRefusal:
    """Decide what to run for a task or project launch, and return it as data.

    The one launch path: ``witan task run``, ``witan project run`` and the
    language server's ``witan.runTask``/``witan.runProject`` all call it, so it
    prints nothing and never exits. A task launch claims the task, bare, after
    the workspace check, so a refusal never leaves a lease behind. A project
    launch claims nothing; its prompt has the agent claim each task it starts.

    :param kind: ``task`` or ``project``.
    :param slug: The ``tk-`` or ``wp-`` slug.
    :param server: The tool dispatcher (``witan.server`` or a remote proxy).
    :param cfg: The resolved config of the invoking folder; ``target_name`` is
        the target ``server`` dispatches to.
    :param workspace_folders: The open folders, the invoking one first.
    :param started_with_target: Whether this process was given ``--target``.
    :param agent: Agent CLI, overriding ``cfg.agent``.
    :param model: Model, overriding ``cfg.model``.
    :param claim: For a task, claim it before launching.
    :param force: For a task, take the claim from another holder's live lease.
    :returns: The launch spec, or a refusal saying why there is nothing to run.
    :rtype: LaunchSpec | LaunchRefusal
    """
    if kind == "task":
        record = _fn(server.task_get)(slug=slug)
        repos = [record["repo"]] if record and record.get("repo") else []
    else:
        record = _fn(server.workflow_project_get)(slug=slug)
        repos = (record or {}).get("repos") or []
    if not record:
        return _refusal(kind, slug, "not_found", f"No {kind} {slug!r}.")

    folder = _launch_folder(workspace_folders, repos)
    if folder is None:
        refusal = _refusal(
            kind,
            slug,
            "no_workspace",
            (
                f"{slug} belongs to {', '.join(repos)}, and no open folder is a "
                "checkout of it."
            )
            if repos
            else f"No folder is open to launch {slug} in.",
        )
        refusal["repos"] = list(repos)
        return refusal

    # A named target is pinned in `env`, so the agent follows it whatever the
    # folder matches. The local store cannot be pinned (it has no name, and an
    # empty WITAN_TARGET means auto-detect), so the agent only reaches it if
    # its own matching from the folder does.
    if not cfg.target_name and (agent_target := config_module.folder_target(folder)):
        refusal = _refusal(
            kind,
            slug,
            "target_mismatch",
            f"This launch reads {slug} from the local store, but an agent "
            f"started in {folder} would use the {agent_target!r} target, where "
            f"it would not find what it was given. Launch with --target "
            f"{agent_target} to use that target instead.",
        )
        refusal["target"] = agent_target
        return refusal

    env, unset_env = launch_env(cfg)
    # The agent's own proxy sends this id, and the claim or memory lookup made
    # on its behalf below is logged under it, so the agent's reads join to the
    # surfacing that preceded them.
    env[CLIENT_INSTANCE_ENV] = new_client_instance()

    claim_result = None
    if kind == "task" and claim:
        # No explicit assignee either: only a defaulted holder is one the
        # launched agent may renew as its own (`_own_claim`). No steal offer:
        # it would print to stdout, and `force` is how a launcher steals.
        with (
            bare_session_env(),
            prompts_off(),
            launched_client_instance(env[CLIENT_INSTANCE_ENV]),
        ):
            res = _fn(server.task_claim)(
                slug=slug, force=force, **_folder_checkout(folder)
            )
        if res is None:
            return _refusal(kind, slug, "not_found", f"No {kind} {slug!r}.")
        # The warning advises passing a session_id, and a bare launch claim is
        # the intent, not the mistake it warns about.
        claim_result = {k: v for k, v in res.items() if k != "warning"}
        if not claim_result["claimed"]:
            holder = claim_result.get("held_by") or claim_result["reason"]
            return _refusal(
                kind,
                slug,
                claim_result["reason"],
                f"Could not claim {slug} ({holder}).",
                claim_result,
            )

    if kind == "task":
        prompt = _run_prompt(
            record,
            claimed=claim,
            memories=_prompt_memories(server, slug, claim_result),
            overlaps=(claim_result or {}).get("overlaps"),
        )
    else:
        tasks = _fn(server.task_ready)(
            project_slug=slug, limit=_PROJECT_PROMPT_TASK_LIMIT
        )
        with bare_session_env(), launched_client_instance(env[CLIENT_INSTANCE_ENV]):
            memories = _project_prompt_memories(server, slug, tasks)
        prompt = _project_run_prompt(record, tasks, memories)

    resolved_agent = agent or cfg.agent
    resolved_model = model or cfg.model
    return LaunchSpec(
        version=LAUNCH_SPEC_VERSION,
        kind=kind,
        slug=slug,
        launchable=True,
        argv=_agent_argv(resolved_agent, resolved_model, prompt),
        cwd=str(folder),
        env=env,
        unset_env=unset_env,
        # A handoff never claims: a prefilled prompt nobody submits would hold
        # the lease for an hour, with no failed launch to release it on.
        handoff=claim_result is None
        and _handoff_allowed(workspace_folders, started_with_target),
        prompt=prompt,
        agent=resolved_agent,
        model=resolved_model,
        claim=claim_result,
    )


def check_emit_spec_args(emit_spec: bool, *, slug: str | None, dry_run: bool) -> None:
    """Exit non-zero when ``--emit-spec`` comes without a slug or with ``--dry-run``.

    A spec describes one launch, so the picker has nothing to emit, and a dry
    run prints a prompt where the caller is waiting for JSON.

    :param emit_spec: Whether ``--emit-spec`` was passed.
    :param slug: The slug the command was given, if any.
    :param dry_run: Whether ``--dry-run`` was passed.
    """
    if emit_spec and (dry_run or not slug):
        print_error("--emit-spec needs a slug and cannot be combined with --dry-run.")
        raise SystemExit(1)


def _deliver(spec: LaunchSpec | LaunchRefusal, cfg, *, emit_spec: bool) -> None:
    """Print ``spec`` as JSON, or run it, exiting non-zero on a refusal."""
    if emit_spec:
        print(json.dumps(spec, indent=2))
        if not spec["launchable"]:
            raise SystemExit(1)
        return
    if not spec["launchable"]:
        print_error(spec["message"])
        if spec["claim"] and spec["claim"].get("remedy"):
            stderr_console.print(f"  [yellow]{esc(spec['claim']['remedy'])}[/yellow]")
        raise SystemExit(1)
    if spec["claim"]:
        console.print(
            f"[cyan]Claimed {spec['slug']} (assignee={spec['claim']['assignee']}).[/cyan]"
        )
    _launch_agent(
        cfg,
        spec["agent"],
        spec["model"],
        spec["prompt"],
        False,
        cwd=spec["cwd"],
        env=agent_environ(spec["env"], spec["unset_env"]),
    )


def _warn_open_blockers(s, t: dict) -> None:
    open_blockers = [
        b
        for b in (t.get("blocked_by") or [])
        if (_fn(s.task_get)(slug=b) or {}).get("status") != "closed"
    ]
    if open_blockers:
        stderr_console.print(
            f"[yellow]Warning: open blockers: {', '.join(open_blockers)}[/yellow]"
        )


def _run_task_slug(
    slug: str,
    *,
    cfg,
    agent: str | None,
    model: str | None,
    claim: bool,
    dry_run: bool,
    force: bool = False,
    emit_spec: bool = False,
) -> None:
    s = _srv()
    t = _fn(s.task_get)(slug=slug)
    if t:
        _warn_open_blockers(s, t)
    if dry_run:
        if not t:
            print_error(f"No task {slug!r}.")
            raise SystemExit(1)
        _print_prompt(_run_prompt(t, claimed=claim, memories=_prompt_memories(s, slug)))
        return
    spec = build_launch_spec(
        "task",
        slug,
        server=s,
        cfg=cfg,
        workspace_folders=[Path.cwd()],
        started_with_target=selected_target() is not None,
        agent=agent,
        model=model,
        claim=claim,
        force=force,
    )
    _deliver(spec, cfg, emit_spec=emit_spec)


def _run_project_slug(
    slug: str,
    *,
    cfg,
    agent: str | None,
    model: str | None,
    dry_run: bool,
    emit_spec: bool = False,
) -> None:
    s = _srv()
    if dry_run:
        p = _fn(s.workflow_project_get)(slug=slug)
        if not p:
            print_error(f"No project {slug!r}.")
            raise SystemExit(1)
        tasks = _fn(s.task_ready)(project_slug=slug, limit=_PROJECT_PROMPT_TASK_LIMIT)
        _print_prompt(
            _project_run_prompt(p, tasks, _project_prompt_memories(s, slug, tasks))
        )
        return
    spec = build_launch_spec(
        "project",
        slug,
        server=s,
        cfg=cfg,
        workspace_folders=[Path.cwd()],
        started_with_target=selected_target() is not None,
        agent=agent,
        model=model,
    )
    _deliver(spec, cfg, emit_spec=emit_spec)


def _merge_prompts(prompts: list[str], kind: str) -> str:
    """Combine multiple task/project prompts into one consolidated session prompt."""
    header = f"Work on these {len(prompts)} {kind}s in order. Complete each before moving to the next.\n"
    separator = "\n" + "─" * 60 + "\n"
    return header + separator.join(prompts)
