"""Live state of the GitHub PR or issue a task or project links to.

A task's description and comments, and a project's description, are written
once and then drift. An agent reading them later repeats them as current fact:
"blocked on sign-off" from a comment the user has since answered, "PR in draft"
for one that merged yesterday. The linked PR's own state is the live signal,
so it is fetched and shown next to the stored text, stamped with when it was
fetched.

── WHY ON THIS MACHINE, WITH THE USER'S ``gh`` ──
A deployed witan holds no GitHub credential on anyone's behalf, so it cannot
fetch this. Everything that renders it runs locally: the context hook, the CLI,
and ``witan serve`` (the stdio server, or the forwarder every agent call to a
deployment passes through, ``witan.remote.serve``). Each of them runs ``gh``
as the user, so a private repo resolves exactly when the user can see it.

Every failure (no ``gh``, ``gh`` not logged in, a timeout, a URL that is not a
GitHub PR or issue) comes back as ``{"error": ...}``. Nothing here raises,
because every caller is a read that must not fail over an advisory field.

Set ``WITAN_LIVE_PR=0`` to turn the lookups off.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from witan_core.observability import get_logger

from . import session_state

logger = get_logger("witan.live_pr")

# One `gh pr view` measured at ~2s and one `gh issue view` at ~2.5s against
# github.com (2026-10-23). 5s leaves room for a slow network while keeping one
# lookup well inside the context hook's 30s shared deadline.
_GH_TIMEOUT_SECONDS = 5.0

# A burst of prompts, or an agent re-reading the same task, should not pay a
# GitHub round trip each time. Two minutes is short next to how fast a PR's
# state changes in practice, and the rendered line carries its fetch time.
_CACHE_TTL_SECONDS = 120.0

# The URL fields that link a record to GitHub, by the tool that returns it.
LINKED_FIELDS = {"task_get": "external_uri", "workflow_project_get": "github_pr"}

_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?P<kind>pull|issues)/(?P<number>\d+)"
    r"(?:[/?#].*)?$"
)

_PR_FIELDS = "number,state,isDraft,reviewDecision,mergedAt,statusCheckRollup"
_ISSUE_FIELDS = "number,state,closedAt"

# CheckRun conclusions that do not fail a PR. Everything else that is
# COMPLETED (FAILURE, TIMED_OUT, CANCELLED, ACTION_REQUIRED, STARTUP_FAILURE,
# STALE) counts as a failure.
_PASSING_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
# Commit-status (StatusContext) states that are still waiting.
_PENDING_STATES = frozenset({"PENDING", "EXPECTED"})

# Set by `witan serve --transport stdio`. The same tool functions are what the
# deployed server serves, and there they must not shell out to a `gh` that
# would act as whoever runs the pod.
_annotate_tool_results = False


class _Unavailable(Exception):
    """A lookup that could not produce a state; its message is the reason."""


def enabled() -> bool:
    return os.environ.get("WITAN_LIVE_PR", "1") != "0"


def parse(url: str) -> tuple[str, str, str] | None:
    """``(kind, canonical url, "owner/repo#N")`` for a GitHub PR or issue URL.

    ``kind`` is ``"pr"`` or ``"issue"``. Accepts a URL without a scheme
    (``github.com/o/r/pull/1``, the form the project-tracker skill shows) and
    anything after the number (``/files``, ``#issuecomment-…``).
    """
    match = _URL.match(url.strip())
    if not match:
        return None
    owner, repo, kind, number = match.group("owner", "repo", "kind", "number")
    return (
        "pr" if kind == "pull" else "issue",
        f"https://github.com/{owner}/{repo}/{kind}/{number}",
        f"{owner}/{repo}#{number}",
    )


def live_state(url: str) -> dict[str, Any]:
    """The current state of the PR or issue at ``url``; never raises.

    A PR answers ``{"kind": "pr", "url", "ref", "state", "draft",
    "review_decision", "merged_at", "checks": {"pass", "fail", "pending"},
    "fetched_at"}``, where ``state`` is ``open``, ``closed`` or ``merged``. An
    issue answers ``{"kind": "issue", "url", "ref", "state", "closed_at",
    "fetched_at"}``. Anything else is ``{"error": "<why>"}``.

    ``fetched_at`` is when GitHub was asked, so a cached answer carries the
    time it was true at rather than the time it was read back.
    """
    try:
        parsed = parse(url)
        if parsed is None:
            return {"error": f"not a GitHub PR or issue URL: {url}"}
        kind, canonical, ref = parsed
        if (cached := _read_cache(canonical)) is not None:
            return cached
        if kind == "pr":
            raw = _gh("pr", "view", canonical, "--json", _PR_FIELDS)
            state = _pr_state(raw)
        else:
            raw = _gh("issue", "view", canonical, "--json", _ISSUE_FIELDS)
            state = {
                "state": str(raw["state"]).lower(),
                "closed_at": raw.get("closedAt"),
            }
        state = {
            "kind": kind,
            "url": canonical,
            "ref": ref,
            **state,
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _write_cache(canonical, state)
        return state
    except _Unavailable as exc:
        logger.debug("witan.live_pr.unavailable", url=url, reason=str(exc))
        return {"error": str(exc)}
    except Exception as exc:  # an advisory field never fails a read
        logger.debug("witan.live_pr.failed", url=url, exc_info=True)
        return {"error": f"live state lookup failed: {exc}"}


def annotate(record: Any, field: str) -> Any:
    """Add ``live_pr`` to ``record`` when its ``field`` is a GitHub URL.

    Returns ``record`` unchanged when it is not a dict, carries no such URL
    (a URL that is not GitHub's is not an error worth reporting on every
    read), or lookups are off.
    """
    if not enabled() or not isinstance(record, dict):
        return record
    url = record.get(field)
    if not isinstance(url, str) or parse(url) is None:
        return record
    return {**record, "live_pr": live_state(url)}


def annotate_tool_results() -> None:
    """Make :func:`tool_result` annotate; called by the local stdio server only."""
    global _annotate_tool_results
    _annotate_tool_results = True


def tool_result(tool: str, record: Any) -> Any:
    """``record`` as tool ``tool`` returns it: annotated on a local server."""
    if not _annotate_tool_results:
        return record
    return annotate(record, LINKED_FIELDS[tool])


def describe(state: dict[str, Any]) -> str | None:
    """One line for a successful :func:`live_state`, or None for an error.

    e.g. ``PR mitodl/agent-kit#512: open (draft), review: CHANGES_REQUESTED,
    checks: 7 pass / 1 fail (live, 14:02 UTC)``. UTC, like the comment dates
    the context hook shows next to it, so the two can be compared.
    """
    if "error" in state:
        return None
    label = "PR" if state["kind"] == "pr" else "Issue"
    parts = [f"{label} {state['ref']}: {state['state']}"]
    if state.get("draft") and state["state"] == "open":
        parts[0] += " (draft)"
    if state.get("review_decision"):
        parts.append(f"review: {state['review_decision']}")
    checks = state.get("checks") or {}
    counts = [f"{checks[k]} {k}" for k in ("pass", "fail", "pending") if checks.get(k)]
    if counts:
        parts.append(f"checks: {' / '.join(counts)}")
    fetched = datetime.fromisoformat(state["fetched_at"]).astimezone(UTC)
    return f"{', '.join(parts)} (live, {fetched:%H:%M} UTC)"


def _gh(*args: str) -> dict[str, Any]:
    gh = shutil.which("gh")
    if gh is None:
        raise _Unavailable("gh is not on PATH")
    try:
        proc = subprocess.run(
            [gh, *args],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"},
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise _Unavailable(f"gh timed out after {_GH_TIMEOUT_SECONDS:g}s") from None
    except OSError as exc:
        raise _Unavailable(f"gh could not run: {exc}") from None
    if proc.returncode != 0:
        reason = (proc.stderr.strip().splitlines() or ["no output"])[0]
        raise _Unavailable(f"gh exited {proc.returncode}: {reason}")
    return json.loads(proc.stdout)


def _pr_state(raw: dict[str, Any]) -> dict[str, Any]:
    checks = {"pass": 0, "fail": 0, "pending": 0}
    for item in raw.get("statusCheckRollup") or []:
        checks[_check_outcome(item)] += 1
    return {
        "state": str(raw["state"]).lower(),
        "draft": bool(raw.get("isDraft")),
        "review_decision": raw.get("reviewDecision") or None,
        "merged_at": raw.get("mergedAt"),
        "checks": checks,
    }


def _check_outcome(item: dict[str, Any]) -> str:
    """``pass``/``fail``/``pending`` for one ``statusCheckRollup`` entry.

    The rollup mixes two shapes: a ``CheckRun`` (Actions and other apps) has
    ``status`` and ``conclusion``; a ``StatusContext`` (the commit-status API)
    has only ``state``.
    """
    if item.get("__typename") == "StatusContext":
        state = item.get("state")
        if state == "SUCCESS":
            return "pass"
        return "pending" if state in _PENDING_STATES else "fail"
    if item.get("status") != "COMPLETED":
        return "pending"
    return "pass" if item.get("conclusion") in _PASSING_CONCLUSIONS else "fail"


def _cache_file(url: str) -> Path:
    digest = hashlib.sha1(url.encode()).hexdigest()[:16]
    return session_state.session_state_dir() / f"witan-livepr-{digest}.json"


def _read_cache(url: str) -> dict[str, Any] | None:
    """A cached answer for ``url`` this user wrote inside the TTL, else None.

    The file sits at a predictable name in a shared temp dir, so anything
    about it is checked rather than trusted: another local user could plant
    one, and a malformed state would crash :func:`describe` in the hook.
    A file this user does not own, a stamp in the future, or a state that is
    not one :func:`live_state` could have produced all count as a miss.
    """
    path = _cache_file(url)
    try:
        if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
            logger.debug("witan.live_pr.cache_foreign_owner", path=str(path))
            return None
        data = json.loads(path.read_text())
        age = time.time() - float(data["stamp"])
        state = data["state"]
        if 0 <= age < _CACHE_TTL_SECONDS and _well_formed(state, url):
            return state
    except FileNotFoundError:  # the first lookup of this URL
        return None
    except Exception:  # corrupt cache → ask GitHub
        logger.debug("witan.live_pr.cache_unreadable", url=url, exc_info=True)
    return None


def _well_formed(state: object, url: str) -> bool:
    if not isinstance(state, dict) or state.get("url") != url:
        return False
    if state.get("kind") not in ("pr", "issue"):
        return False
    if not all(isinstance(state.get(k), str) for k in ("ref", "state", "fetched_at")):
        return False
    datetime.fromisoformat(state["fetched_at"])  # raises on garbage
    checks = state.get("checks", {})
    return isinstance(checks, dict) and all(isinstance(v, int) for v in checks.values())


def _write_cache(url: str, state: dict[str, Any]) -> None:
    session_state.write_private(
        _cache_file(url), json.dumps({"stamp": time.time(), "state": state})
    )
