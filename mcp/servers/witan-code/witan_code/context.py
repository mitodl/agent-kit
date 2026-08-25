"""Self-contained UserPromptSubmit status block for witan-code.

Mirrors the shape of ``witan``'s own ``inject-context`` hook, but is a
deliberately independent implementation (no cross-package import — see
``graph.py``'s docstring) so witan-code stays fully usable, and self-announcing,
when installed standalone without ``witan``.

Tells the agent whether the current repo has a code graph ready to query (and
its rough size/freshness) — without this, an agent has no signal that
``code_*`` tools exist or are populated, short of trying one and seeing what
comes back.

The block deliberately names the *unlock step* rather than stating a
preference. Measured over 50 sessions in this repo that received the earlier
"prefer ``code_search_symbol`` ... over grep" wording: the ``code_*`` tools
arrived DEFERRED (names only, no schema — a ``ToolSearch`` round-trip short of
callable) in 50 of 50, while Grep/Read/Glob were always loaded. Those sessions
produced 5 ``code_*`` calls against 802 Grep/Read/Glob/Explore calls, and 46 of
50 never called a ``code_*`` tool at all. A preference for a tool the agent
cannot see in its tool list is not actionable, so the block leads with the
``ToolSearch`` that makes the tools callable and then gives a call template to
fill in.

Kept short on purpose: this is prepended to *every* prompt, so tokens spent
here are spent for the life of every session.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import time
from pathlib import Path

from . import config as cfg_module
from . import repo as repo_module
from . import store as store_module

# Matches the lock directory hooks.session_init() creates around a background
# SessionStart index, so this hook can report "indexing in progress" instead
# of a misleadingly empty/stale store. Keyed on a hash of the project
# directory (not the raw sanitized path) so two distinct paths can't collide
# on the same lock file (e.g. "/tmp/a/b" and "/tmp/a_b" both sanitizing to
# "_tmp_a_b") and so a deep/long checkout path can't blow past a filesystem's
# filename length limit and silently fail the `mkdir`.
_LOCK_PREFIX = "codegraph-init-"


def _lock_digest(project_dir: Path) -> str:
    return hashlib.sha256(str(project_dir).encode()).hexdigest()[:16]


def _project_dir() -> Path:
    try:
        cwd = os.getcwd()
    except OSError:  # e.g. the cwd was deleted out from under this process
        cwd = "/"
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", cwd))


def _lock_path(project_dir: Path) -> Path:
    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    return tmp / f"{_LOCK_PREFIX}{_lock_digest(project_dir)}.lock"


def indexing_in_progress() -> bool:
    """Whether hooks.session_init()'s background index is still running."""
    return _lock_path(_project_dir()).is_dir()


# The one query form that survives not knowing the MCP server's tool prefix.
# `select:` needs exact names (`mcp__witan__code_find_definition`), but the
# prefix depends on what the user named the server in their MCP config, and a
# bare `select:code_find_definition` matches nothing. The `+code_` form
# requires "code_" in the name and ranks by the remaining terms, so it resolves
# under any prefix and puts the three workhorse tools first.
_TOOLSEARCH_QUERY = '`ToolSearch(query="+code_ find_definition callers impact")`'


def _coverage_line(store: store_module.StoreRef, cfg: cfg_module.Config) -> str:
    """One line on whether cross-repo resolution can answer anything here.

    This is the fact an agent cannot infer and gets silently wrong in one
    direction: ``code_interface_consumers`` and friends answering nothing is
    indistinguishable from a genuine "nothing consumes this". Two different
    things cause it, so both are checked.

    HOW MANY OTHER REPOS ARE INDEXED is cheap — a glob locally, one cached
    graph listing on the cluster, no store reads either way.

    WHETHER THE BRIDGE GRAPH OPENS costs one query, because every
    ``code_interface_*`` tool reads that one derived store and nothing else
    does. It is not covered by the repo count: this line claimed cross-repo
    resolution worked on every prompt for six weeks while the bridge had been
    unreadable since an omnigraph upgrade, which is worse than saying nothing
    — an agent that believes it concludes a contract has no consumers when the
    truth is that the query could not run. The probe is cached
    (:func:`_bridge_ok`) so the cost is one query per TTL, not per prompt.
    """
    try:
        others = [ref for ref in store_module.per_repo_stores(cfg) if ref != store]
    except OSError:  # degrade to silence, never blank the block
        return ""
    if _bridge_ok(cfg) is False:
        return (
            "The cross-repo bridge graph cannot be read, so every "
            "`code_interface_*` / `code_cross_repo_impact` call FAILS — do not "
            "read their absence as absence of consumers. `code_store_health` "
            "for why; `witan-code doctor` to fix."
        )
    if not others:
        return (
            "No other repo is indexed: `code_interface_*` return `[]` here — "
            "absence of data, not absence of consumers."
        )
    return (
        f"{len(others)} other repos indexed, so cross-repo `code_interface_*` resolve."
    )


# One bridge probe per this many seconds, cached ON DISK.
#
# An in-process memo would cache nothing here: `witan-code inject-context` is a
# fresh process per prompt, so a module-level dict is born and dies inside one
# hook run. The hook already pays one store query for the repo's file count;
# adding a second, uncached, on every prompt is a cost the block is not worth.
#
# The answer only changes when a store is rebuilt or omnigraph is upgraded, so
# a few minutes of staleness is the same exposure the file count already
# carries. The failure direction that matters is the safe one: a cache miss
# probes, and an unwritable cache just means the next prompt probes again.
_BRIDGE_PROBE_TTL = 300.0


def _bridge_probe_path(cfg: cfg_module.Config) -> Path:
    """Where the cached bridge verdict lives — beside the session lock, in TMPDIR.

    Keyed by a hash of the code dir rather than the path itself: two code dirs
    must not collide on one cache entry, and a long path must not blow past a
    filename limit — the same reasoning, and the same digest, as
    :func:`_lock_path`.
    """
    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    digest = hashlib.sha256(str(cfg.code_dir).encode()).hexdigest()[:16]
    return tmp / f"codegraph-bridge-{digest}.probe"


def _bridge_ok(cfg: cfg_module.Config) -> bool | None:
    """Whether the cross-repo bridge graph opens; ``None`` when there isn't one.

    ``None`` and ``False`` are deliberately distinct. A repo indexed on its own,
    with no bridge yet, has nothing broken — the repo-count line already covers
    it. A bridge that exists and will not open is a live failure.
    """
    ref = store_module.bridge_store(cfg)
    if not ref.exists(cfg):
        return None
    cache = _bridge_probe_path(cfg)
    try:
        if time.time() - cache.stat().st_mtime < _BRIDGE_PROBE_TTL:
            return cache.read_text().strip() == "ok"
    except OSError:
        pass  # no cache, or an unreadable one: probe and rewrite it
    ok = store_module.store_health(ref, cfg, bridge=True).ok
    try:
        cache.write_text("ok" if ok else "broken")
    except OSError:
        # A full or read-only TMPDIR costs a probe per prompt, not a wrong
        # answer. Never let caching a verdict fail the block that reports it.
        pass
    return ok


def inject_context() -> str:
    """A short markdown status block, or "" when there's nothing worth saying.

    Silent when the repo has neither a store nor an index in flight (nothing
    to report), so this hook adds no noise for repos that don't use witan-code.
    """
    cfg = cfg_module.load()
    slug = repo_module.detect()
    if slug is None:
        return ""

    store = store_module.store_for_repo(slug, cfg)
    in_progress = indexing_in_progress()

    if not store.exists(cfg):
        if not in_progress:
            return ""
        return (
            "## Code Graph\n\n"
            f"Indexing `{slug}` for the first time in the background — "
            "`code_*` tools may return partial or empty results until it "
            "finishes.\n"
        )

    repo_uri = store_module.repo_for_store(store, cfg)
    health = store_module.store_health(store, cfg)
    if not health.ok and not in_progress:
        # An unreadable store used to render as "? files", which reads as a
        # cosmetic gap in an otherwise working index. It is not: every `code_*`
        # tool against this repo errors. Say that instead of the size/freshness
        # block, which describes a graph nothing can query.
        #
        # Deferring to `in_progress` covers the one benign way to read this: a
        # store directory that exists because an index is part-way through
        # creating it. That is a graph on its way in, not one that is broken,
        # and the in-flight message below already tells the agent not to trust
        # what it holds yet.
        return (
            "## Code Graph\n\n"
            f"`{repo_uri}`'s code graph CANNOT BE READ — every `code_*` tool "
            "against this repo will fail. Use grep/Read here, and run "
            "`witan-code doctor` to see the reason and the fix.\n"
        )
    files = health.files
    # No freshness line for a cluster graph: mtime is a property of a store
    # directory and there isn't one. Same degraded rendering as a mid-walk
    # failure — the block is worth having without it.
    try:
        _, mtime = store.stats()
    except OSError:  # e.g. a file vanished mid-walk — degrade, don't blank the block
        mtime = None
    if mtime is None:
        freshness = ""
    else:
        stamp = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        freshness = f", last updated {stamp}"
    lines = [
        "## Code Graph",
        "",
        f"`{repo_uri}` is indexed: {'?' if files is None else files} files{freshness}.",
    ]
    if in_progress:
        lines.append("A background reindex is currently running.")
    coverage = _coverage_line(store, cfg)
    if coverage:
        lines.append(coverage)
    lines.append(
        "`code_*` tools may not be in your tool list — load them with "
        f"{_TOOLSEARCH_QUERY}, then use them instead of grep: "
        '`code_find_definition(name="X")` → `symbol_id` → `code_callers` / '
        "`code_impact` (blast radius before editing). More: `/witan-code`."
    )
    return "\n".join(lines) + "\n"
