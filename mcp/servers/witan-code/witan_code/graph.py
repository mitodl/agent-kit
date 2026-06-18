import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

# omnigraph local stores use optimistic concurrency (Lance manifest versions) and
# are NOT safe for concurrent writers — a manual index racing the
# PostToolUse/SessionStart reindex hooks can leave HEAD ahead of the manifest.
# We serialize writes with a per-store advisory lock (prevention) and, as a
# safety net, retry transient "stale view" conflicts and `omnigraph repair`
# stores already in the drifted state.
_WRITE_SUBCOMMANDS = {"mutate", "load"}
_RETRYABLE = ("stale view", "manifest table version", "refresh and retry")
_NEEDS_REPAIR = ("ahead of manifest", "omnigraph repair")
_MAX_ATTEMPTS = 8


class OmnigraphClient:
    """Subprocess wrapper for the omnigraph CLI.

    Copied verbatim from witan (queries_dir default adjusted) so the
    two Layer packages stay independent — no cross-package imports.
    """

    def __init__(
        self,
        graph_uri: str,
        queries_dir: Path,
        token: str | None = None,
    ) -> None:
        self.graph_uri = graph_uri
        self.queries_dir = queries_dir
        self.token = token
        self._binary = self._find_binary()

    # ── Public API ────────────────────────────────────────────────

    def read(
        self,
        query_file: str,
        query_name: str,
        params: dict,
    ) -> list[dict]:
        """Run a named read query. Returns a list of result rows."""
        result = self._run(
            "query",
            "--query",
            str(self.queries_dir / query_file),
            query_name,
            "--params",
            json.dumps(params),
            "--format",
            "json",
        )
        if not result.strip():
            return []
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"omnigraph returned non-JSON: {result!r}") from exc
        # v0.7.0 wraps results in {rows: [...], columns: [...], ...}
        rows = parsed.get("rows", parsed) if isinstance(parsed, dict) else parsed
        # strip alias prefixes: "s.name" → "name"
        return [{k.split(".", 1)[-1]: v for k, v in row.items()} for row in rows]

    def change(
        self,
        query_file: str,
        query_name: str,
        params: dict,
    ) -> None:
        """Run a named mutation query."""
        self._run(
            "mutate",
            "--query",
            str(self.queries_dir / query_file),
            query_name,
            "--params",
            json.dumps(params),
        )

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

    # ── Internals ─────────────────────────────────────────────────

    def _run(self, subcommand: str, *args: str) -> str:
        cmd = [self._binary, subcommand, "--store", self.graph_uri, *args]
        env = dict(os.environ)
        if self.token:
            env["OMNIGRAPH_SERVER_BEARER_TOKEN"] = self.token

        lock_fh = self._acquire_write_lock(subcommand)
        try:
            for attempt in range(_MAX_ATTEMPTS):
                result = subprocess.run(cmd, capture_output=True, text=True, env=env)
                if result.returncode == 0:
                    return result.stdout
                err = result.stderr
                if attempt + 1 < _MAX_ATTEMPTS:
                    if any(m in err for m in _NEEDS_REPAIR):
                        self._repair(env)
                        continue
                    if any(m in err for m in _RETRYABLE):
                        time.sleep(0.05 * (attempt + 1))
                        continue
                raise RuntimeError(
                    f"omnigraph {subcommand} failed (exit {result.returncode}):\n"
                    f"{err.strip()}"
                )
            raise AssertionError("unreachable")  # pragma: no cover
        finally:
            if lock_fh is not None:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                lock_fh.close()

    def _acquire_write_lock(self, subcommand: str):
        """Hold a per-store exclusive lock for write subcommands (local stores)."""
        if subcommand not in _WRITE_SUBCOMMANDS or self.graph_uri.startswith(
            ("http://", "https://", "s3://")
        ):
            return None
        lock_path = Path(f"{self.graph_uri}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "w")  # noqa: SIM115 — released in _run's finally
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    def _repair(self, env: dict) -> None:
        """Reconcile manifest/HEAD drift so the retried write can proceed."""
        subprocess.run(
            [self._binary, "repair", "--store", self.graph_uri, "--confirm", "--force"],
            capture_output=True,
            text=True,
            env=env,
        )

    @staticmethod
    def _find_binary() -> str:
        binary = shutil.which("omnigraph")
        if binary is None:
            raise RuntimeError(
                "omnigraph binary not found on PATH. "
                "Run mcp/servers/witan-code/install.sh first."
            )
        return binary
