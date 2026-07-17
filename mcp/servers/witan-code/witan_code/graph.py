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

__all__ = ["OmnigraphClient"]


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
