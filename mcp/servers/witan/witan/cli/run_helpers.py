"""Shared helpers for picking, launching, and prompting agent run sessions."""

from __future__ import annotations

import subprocess

from .. import envelope, provenance
from ..readiness import CLAIM_LEASE_SECONDS
from ._common import (
    _fn,
    _srv,
    console,
    esc,
    print_error,
    stderr_console,
)

# The size of a claim's list, for a prompt with no claim to take one from.
_PROMPT_MEMORY_LIMIT = 3
_PROMPT_EXCERPT_CHARS = 280


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


def _launch_agent(
    cfg, resolved_agent: str, resolved_model: str | None, prompt: str, dry_run: bool
) -> None:
    if dry_run:
        # Not esc(): a dry run exists to show the exact text the agent will
        # receive, so it must not be shown with escapes in it. All three of
        # Rich's substitutions are off — `emoji=False` because a prompt saying
        # `:warning:` renders as ⚠ while the agent receives the literal
        # characters, which is the same class of lie the escaping fixes.
        console.print(prompt, markup=False, emoji=False, highlight=False)
        return
    cmd = [resolved_agent]
    if resolved_model:
        cmd += ["--model", resolved_model]
    cmd.append(prompt)
    target_info = f" [{cfg.target_name}]" if cfg.target_name else ""
    model_info = f" --model {resolved_model}" if resolved_model else ""
    console.print(f"[dim]Launching: {resolved_agent}{model_info}{target_info}[/dim]")
    try:
        subprocess.run(cmd, check=False)
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
    except Exception as exc:  # noqa: BLE001 — never block the launch
        console.print(f"[yellow]Skipping memories: {esc(str(exc))}[/yellow]")
        return []
    return [
        {**m, "excerpt": m["content"][:_PROMPT_EXCERPT_CHARS]}
        for m in result["memories"]
    ]


def _memory_lines(memories: list[dict]) -> list[str]:
    """The prompt section for ``memories``, or nothing when there are none.

    Attributed and labelled as notes, and each collapsed to one line, because
    anyone on the team can write a memory and this reaches an agent's first
    turn, where a line of text would otherwise read as part of the assignment.
    """
    if not memories:
        return []
    lines = [
        "",
        (
            f"Team memories that match this task. {provenance.TEAMMATE_NOTES} "
            "The full text of each is in memory_get:"
        ),
    ]
    for m in memories:
        author, title, excerpt = (
            " ".join(m[field].split()) for field in ("author", "title", "excerpt")
        )
        lines += [f"  {m['slug']} ({author}): {title}", f"    {excerpt}"]
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


def _run_task_slug(
    slug: str,
    *,
    cfg,
    agent: str | None,
    model: str | None,
    claim: bool,
    dry_run: bool,
    force: bool = False,
) -> None:
    resolved_agent = agent or cfg.agent
    resolved_model = model or cfg.model

    s = _srv()
    t = _fn(s.task_get)(slug=slug)
    if not t:
        print_error(f"No task {slug!r}.")
        raise SystemExit(1)

    open_blockers = [
        b
        for b in (t.get("blocked_by") or [])
        if (_fn(s.task_get)(slug=b) or {}).get("status") != "closed"
    ]
    if open_blockers:
        console.print(
            f"[yellow]Warning: open blockers: {', '.join(open_blockers)}[/yellow]"
        )

    if dry_run:
        prompt = _run_prompt(t, claimed=claim, memories=_prompt_memories(s, slug))
        console.print(prompt, markup=False, emoji=False, highlight=False)
        return

    res = None
    if claim:
        # No explicit assignee: task_claim qualifies the author with this agent
        # session. Passing cfg.author here made every parallel session claim
        # under one identity, so the second one renewed the first's lease and
        # was told it had claimed the task — both then worked it.
        res = _fn(s.task_claim)(slug=slug, force=force) or {}
        if not res.get("claimed"):
            reason = res.get("held_by") or res.get("reason") or "unavailable"
            print_error(f"Could not claim {slug} ({reason}).")
            if res.get("remedy"):
                stderr_console.print(f"  [yellow]{esc(res['remedy'])}[/yellow]")
            raise SystemExit(1)
        console.print(f"[cyan]Claimed {slug} (assignee={res.get('assignee')}).[/cyan]")

    prompt = _run_prompt(
        t,
        claimed=claim,
        memories=_prompt_memories(s, slug, res),
        overlaps=(res or {}).get("overlaps"),
    )
    _launch_agent(cfg, resolved_agent, resolved_model, prompt, dry_run)


def _project_run_prompt(p: dict, tasks: list[dict]) -> str:
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


def _run_project_slug(
    slug: str,
    *,
    cfg,
    agent: str | None,
    model: str | None,
    dry_run: bool,
) -> None:
    resolved_agent = agent or cfg.agent
    resolved_model = model or cfg.model

    s = _srv()
    p = _fn(s.workflow_project_get)(slug=slug)
    if not p:
        print_error(f"No project {slug!r}.")
        raise SystemExit(1)

    tasks = _fn(s.task_ready)(project_slug=slug, limit=20)
    prompt = _project_run_prompt(p, tasks)
    _launch_agent(cfg, resolved_agent, resolved_model, prompt, dry_run)


def _merge_prompts(prompts: list[str], kind: str) -> str:
    """Combine multiple task/project prompts into one consolidated session prompt."""
    header = f"Work on these {len(prompts)} {kind}s in order. Complete each before moving to the next.\n"
    separator = "\n" + "─" * 60 + "\n"
    return header + separator.join(prompts)
