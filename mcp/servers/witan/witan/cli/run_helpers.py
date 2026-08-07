"""Shared helpers for picking, launching, and prompting agent run sessions."""

from __future__ import annotations

import subprocess

from ._common import (
    _fn,
    _srv,
    console,
)


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
        console.print(prompt)
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
        console.print(f"[red]Agent {resolved_agent!r} not found on PATH.[/red]")
        raise SystemExit(1) from None


def _run_prompt(t: dict) -> str:
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
    lines += [
        "",
        "Description:",
        t.get("description") or "(none)",
        "",
        f'When complete, close it: task_close(slug="{t["slug"]}", resolution="<what you did>"). '
        f'File any follow-up work with task_create(discovered_from=["{t["slug"]}"], ...).',
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
) -> None:
    resolved_agent = agent or cfg.agent
    resolved_model = model or cfg.model

    s = _srv()
    t = _fn(s.task_get)(slug=slug)
    if not t:
        console.print(f"[red]No task {slug!r}.[/red]")
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

    prompt = _run_prompt(t)
    if dry_run:
        console.print(prompt)
        return

    if claim:
        # No explicit assignee: task_claim qualifies the author with this agent
        # session. Passing cfg.author here made every parallel session claim
        # under one identity, so the second one renewed the first's lease and
        # was told it had claimed the task — both then worked it.
        res = _fn(s.task_claim)(slug=slug) or {}
        if not res.get("claimed"):
            reason = res.get("held_by") or res.get("reason") or "unavailable"
            console.print(f"[red]Could not claim {slug} ({reason}).[/red]")
            if res.get("remedy"):
                console.print(f"  [yellow]{res['remedy']}[/yellow]")
            raise SystemExit(1)
        console.print(f"[cyan]Claimed {slug} (assignee={res.get('assignee')}).[/cyan]")

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
        'When a task is done: task_close(slug="tk-...", resolution="<what you did>").',
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
        console.print(f"[red]No project {slug!r}.[/red]")
        raise SystemExit(1)

    tasks = _fn(s.task_ready)(project_slug=slug, limit=20)
    prompt = _project_run_prompt(p, tasks)
    _launch_agent(cfg, resolved_agent, resolved_model, prompt, dry_run)


def _merge_prompts(prompts: list[str], kind: str) -> str:
    """Combine multiple task/project prompts into one consolidated session prompt."""
    header = f"Work on these {len(prompts)} {kind}s in order. Complete each before moving to the next.\n"
    separator = "\n" + "─" * 60 + "\n"
    return header + separator.join(prompts)
