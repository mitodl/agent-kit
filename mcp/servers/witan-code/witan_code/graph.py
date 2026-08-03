"""witan-code's OmnigraphClient — the shared base plus witan-code's own tail.

The subprocess/lock/retry/admission-cap machinery lives in
``witan_core.omnigraph``; this subclass adds omnigraph *branch* support (each
per-user/per-session code index is isolated on its own store branch) and the
bulk ``load`` used to write thousands of symbol/edge records in one call.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from witan_core.omnigraph import OmnigraphClient as _BaseOmnigraphClient

from . import config as cfg_module
from . import identity as identity_module
from . import views

__all__ = [
    "OmnigraphClient",
    "SharedGraphWriteRefused",
    "check_writable",
    "owns_view",
]


class SharedGraphWriteRefused(RuntimeError):
    """A writer tried to write a view of a shared graph it does not own."""


def owns_view(
    *,
    is_remote: bool,
    branch: str | None,
    cfg: cfg_module.Config,
    actor: str | None,
) -> bool:
    """Whether this process is the single writer of the view it is about to touch.

    The one question both destructive operations turn on — writing a view, and
    purging rows from one — so they ask it in the same place rather than each
    spelling out its own approximation of it. Three cases:

    - **A local store has one user, who is its writer.** Nothing to arbitrate.
    - **CI owns the shared default view.** ``branch is None`` means the write
      targets the store's ``main``, the view every reader falls back to. On the
      cluster that view has exactly one writer, the CI indexer, and it says so
      (``WITAN_CODE_INDEX_ROLE=ci``). Authority is the declared role, never the
      transport: CI is remote too, so "refuse when remote" would block the one
      writer the design depends on.
    - **Each actor owns its own branch views.** Per-user branch views live ON
      the shared graph (DECIDED, Tobias 2026-07-31) — in-flight work being
      visible to other agents as it happens is much of what the shared service
      is for — so isolation cannot come from *where* the view lives. It comes
      from the name: a view is owned by the actor it is prefixed with
      (:mod:`witan_code.views`), and a process writes only views prefixed with
      its own. An un-prefixed branch view on a shared graph is owned by nobody,
      which is the collision this replaced.
    """
    if not is_remote:
        return True
    if branch is None:
        return cfg.is_designated_writer
    return actor is not None and views.owner(branch) == actor


def check_writable(
    *,
    is_remote: bool,
    branch: str | None,
    cfg: cfg_module.Config,
    slug: str,
    actor: str | None = None,
) -> None:
    """Raise :class:`SharedGraphWriteRefused` unless :func:`owns_view` allows it.

    ``actor`` is the identity this process writes as; it defaults to the
    resolved one, so a caller that does not construct view names itself does
    not have to thread it through.

    ``is_remote`` is "is this graph shared", which for a client is a property
    of its store (``client.is_remote``) and for the MCP tier serving somebody
    else's write is true by construction — that is the only reason the request
    exists (:mod:`witan_code.ingest`). Taking the bit rather than the client is
    what lets both ask the same question.
    """
    actor = actor if actor is not None else identity_module.actor_id()
    if owns_view(is_remote=is_remote, branch=branch, cfg=cfg, actor=actor):
        return
    if branch is None:
        raise SharedGraphWriteRefused(
            f"Refusing to index {slug} onto the shared graph's default branch: "
            f"that view is owned by CI, and this process is acting as "
            f"'{cfg.index_role}'. Index a non-default git branch to write an "
            f"isolated branch view, or set "
            f"WITAN_CODE_INDEX_ROLE={cfg_module.INDEX_ROLE_CI} if this IS the "
            f"CI indexer."
        )
    if actor is None:
        raise SharedGraphWriteRefused(
            f"Refusing to write branch view {branch!r} of {slug} to a shared "
            "graph without an identity to own it: another writer on the same "
            "git branch would silently overwrite it. Run `witan login`, or set "
            f"{identity_module.ACTOR_ENV_VAR} for a non-interactive writer."
        )
    raise SharedGraphWriteRefused(
        f"Refusing to write branch view {branch!r} of {slug}: it is owned by "
        f"{views.owner(branch) or 'nobody'}, and this process is {actor}. "
        "Branch views are readable by everyone and writable only by their "
        "owner."
    )


class OmnigraphClient(_BaseOmnigraphClient):
    """The base client, specialized for witan-code (per-repo code-graph stores)."""

    _SETUP_HINT = "witan-code setup (or `witan setup`, if witan is also installed)"

    def __init__(
        self,
        graph_uri: str,
        queries_dir: Path,
        token: str | None = None,
        branch: str | None = None,
        graph_id: str | None = None,
    ) -> None:
        # Target omnigraph branch for query/mutate/load; None = the store's main
        # branch. Loads pass `--from main` so the branch forks lazily on first
        # write (docs/BRANCH_INDEXING.md).
        self.branch = branch
        super().__init__(graph_uri, queries_dir, token, graph_id=graph_id)

    def load(self, records: list[dict], mode: str = "merge") -> None:
        """Bulk-load node/edge records via one ``omnigraph load`` call.

        Each record is a JSONL line: ``{"type": Node, "data": {...}}`` for a
        node or ``{"edge": Edge, "from": key, "to": key}`` for an edge. This
        replaces thousands of per-record ``mutate`` subprocesses with a single
        invocation — essential for indexing large repositories.
        """
        if not records:
            return
        fd, tmp = tempfile.mkstemp(suffix=".jsonl", prefix="codegraph-load-")
        try:
            with os.fdopen(fd, "w") as fh:
                for record in records:
                    fh.write(json.dumps(record))
                    fh.write("\n")
            self._run("load", "--data", tmp, "--mode", mode)
        finally:
            Path(tmp).unlink(missing_ok=True)

    # ── Branch operations ─────────────────────────────────────────

    def list_branches(self) -> list[str]:
        """Names of all branches on this store (includes ``main``)."""
        result = self._run("branch", "list", "--json")
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return []
        rows = parsed.get("branches", parsed) if isinstance(parsed, dict) else parsed
        if not isinstance(rows, list):
            return []
        out: list[str] = []
        for row in rows:
            name = row.get("name") if isinstance(row, dict) else row
            if isinstance(name, str):
                out.append(name)
        return out

    def ensure_branch(self) -> None:
        """Create ``self.branch`` from main if it doesn't exist yet.

        Needed before the first *read* on a new branch — reads never fork
        (only ``load --from`` does), so a read against a missing branch errors.
        """
        if self.branch is None or self.branch in self.list_branches():
            return
        self._run("branch", "create", self.branch, "--from", "main")

    def delete_branch(self, name: str) -> None:
        self._run("branch", "delete", name, "--yes")

    def branch_last_write(self, name: str) -> float | None:
        """When ``name`` was last written, as epoch seconds, or ``None``.

        ``None`` means the branch has no commits *of its own* — every commit
        reachable from it came from the branch it forked off. That is a view
        created but never indexed, and it is not the same as "old": there is no
        branch-creation timestamp anywhere in omnigraph 0.8.1 to age it by. The
        reaper treats it accordingly (:mod:`witan_code.reaper`).

        ``omnigraph branch list`` returns bare names, so staleness has to come
        from the commit log: ``commit list --branch`` returns every reachable
        commit, each tagged with the ``manifest_branch`` it landed on, so the
        branch's own writes are the ones tagged with it.

        Output that isn't the expected JSON shape **raises**, and must not
        degrade to ``None``: ``None`` is load-bearing here — it tells the
        reaper never to touch this view — so a format change or a stray
        warning line on stdout would quietly turn a scheduled reaper into a
        no-op that reports success while branch sprawl grows unbounded. Same
        convention as the base client's :meth:`read`.
        """
        result = self._run("commit", "list", "--branch", name, "--json")
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"omnigraph commit list returned non-JSON for branch "
                f"{name!r}: {result!r}"
            ) from exc
        commits = parsed.get("commits") if isinstance(parsed, dict) else parsed
        if not isinstance(commits, list):
            raise RuntimeError(
                f"omnigraph commit list returned no commit array for branch "
                f"{name!r}: {parsed!r}"
            )
        stamps = [
            row["created_at"]
            for row in commits
            if isinstance(row, dict)
            and row.get("manifest_branch") == name
            and isinstance(row.get("created_at"), (int, float))
        ]
        # created_at is microseconds since the epoch.
        return max(stamps) / 1_000_000 if stamps else None

    def _extra_args(self, subcommand: str) -> list[str]:
        # optimize/cleanup compact the whole store (every branch), not a single
        # one, so they never take --branch even on a branched client. `commit`
        # and `branch` name their branch positionally or as their own flag, so
        # injecting this client's would either duplicate the flag or silently
        # retarget the call.
        if self.branch is None or subcommand in (
            "branch",
            "commit",
            "optimize",
            "cleanup",
        ):
            return []
        if subcommand == "load":
            return ["--branch", self.branch, "--from", "main"]
        return ["--branch", self.branch]
