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

__all__ = ["OmnigraphClient", "SharedGraphWriteRefused", "check_writable"]


class SharedGraphWriteRefused(RuntimeError):
    """A non-writer tried to write a shared graph's default-branch view."""


def check_writable(
    *,
    client: OmnigraphClient,
    branch: str | None,
    cfg: cfg_module.Config,
    slug: str,
) -> None:
    """Refuse to write a shared graph's default-branch view without the role.

    ``branch is None`` means the write targets the store's ``main`` — the view
    every user of a shared cluster graph reads. On the cluster that view has
    exactly one writer, the CI indexer, and it says so
    (``WITAN_CODE_INDEX_ROLE=ci``). Any other process indexing it would publish
    one machine's working tree — whatever revision, whatever uncommitted state
    it happens to be in — to the whole team.

    Authority comes from the declared role, never from the transport: CI is
    remote too, so a blanket "refuse when remote" would block the one writer
    the design depends on (``Config.is_designated_writer``).

    Branch views are exempt: they are written through a branch-scoped client,
    so in-flight work stays isolated from the shared view (see
    docs/BRANCH_INDEXING.md). Whether developer branches belong on the shared
    graph at all, or stay in the local store, is still open — see task
    tk-ci-owns-the-default-branch-code-graph-clients-re-9c90d6 item 3.

    Local stores are unaffected: they have one user, who is their writer.
    """
    if not client.is_remote or branch is not None or cfg.is_designated_writer:
        return
    raise SharedGraphWriteRefused(
        f"Refusing to index {slug} onto the shared graph's default branch: "
        f"that view is owned by CI, and this process is acting as "
        f"'{cfg.index_role}'. Index a non-default git branch to write an "
        f"isolated branch view, or set "
        f"WITAN_CODE_INDEX_ROLE={cfg_module.INDEX_ROLE_CI} if this IS the "
        f"CI indexer."
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

    def _extra_args(self, subcommand: str) -> list[str]:
        # optimize/cleanup compact the whole store (every branch), not a single
        # one, so they never take --branch even on a branched client.
        if self.branch is None or subcommand in ("branch", "optimize", "cleanup"):
            return []
        if subcommand == "load":
            return ["--branch", self.branch, "--from", "main"]
        return ["--branch", self.branch]
