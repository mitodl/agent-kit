"""Rendering a workflow project's autonomy envelope.

The envelope is two lists on a ``WorkflowProject``: ``preauthorized`` (actions
an agent may take without asking) and ``reserved_decisions`` (decisions kept
for a human). Each carries the server-resolved actor who last set it
(``*_set_by``). A pre-authorization only reads as standing approval when that
actor is the project's author: the shared graph lets any user write a project,
so a list someone else set is shown as a suggestion to confirm instead.

The context hook, the CLI and ``witan project run`` all describe the lists
through :func:`describe`, so the three agree on what counts as approval.
"""

from __future__ import annotations

# The context hook injects the envelope on every prompt, so each list is capped
# there; the full lists are one `workflow_project_get` away.
CONTEXT_ITEM_CAP = 5
CONTEXT_ITEM_CHARS = 160

KEYS = ("preauthorized", "reserved_decisions")


def owner_granted(project: dict) -> bool:
    """Whether the project's pre-authorizations were set by its author."""
    set_by = project.get("preauthorized_set_by")
    return bool(set_by) and set_by == project.get("author")


def describe(project: dict, key: str) -> str:
    """The heading for one envelope list, naming who set it."""
    who = project.get(f"{key}_set_by") or "an unknown user"
    if key == "reserved_decisions":
        return f"Reserved for a human (set by {who})"
    if owner_granted(project):
        return f"Pre-authorized by the project author ({who})"
    return (
        f"Suggested pre-authorizations from {who}, not the project author "
        f"(confirm with the user before acting, unless {who} is the user you "
        f"are working for)"
    )


def context_lines(project: dict) -> list[str]:
    """One capped line per non-empty list, for the injected context."""
    out: list[str] = []
    for key in KEYS:
        items = project.get(key) or []
        if not items:
            continue
        shown = [
            item
            if len(item) <= CONTEXT_ITEM_CHARS
            else f"{item[: CONTEXT_ITEM_CHARS - 1]}…"
            for item in items[:CONTEXT_ITEM_CAP]
        ]
        line = f"  {describe(project, key)}: {'; '.join(shown)}"
        if len(items) > CONTEXT_ITEM_CAP:
            line += f" (+{len(items) - CONTEXT_ITEM_CAP} more, workflow_project_get)"
        out.append(line)
    return out
