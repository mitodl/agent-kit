import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

# omnigraph local stores use optimistic concurrency (Lance manifest versions) and
# are NOT safe for concurrent writers — several agents writing the shared graph
# at once can leave HEAD ahead of the manifest. We serialize writes with a
# per-store advisory lock (prevention) and, as a safety net, retry transient
# "stale view" conflicts and `omnigraph repair` stores already in the drifted
# state.
_WRITE_SUBCOMMANDS = {"mutate", "load"}
_RETRYABLE = ("stale view", "manifest table version", "refresh and retry")
_NEEDS_REPAIR = ("ahead of manifest", "omnigraph repair")
_MAX_ATTEMPTS = 8

# omnigraph uses strict single-version storage: a release that bumps the
# internal schema version refuses to open graphs an older binary wrote,
# raising exactly this pair of substrings wrapped in an unhelpful Rust panic +
# backtrace (see docs/user/operations/upgrade.md). Not retryable/repairable.
_STORAGE_VERSION_MISMATCH_MARKERS = ("stamped at internal schema", "reads only")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class OmnigraphConflict(RuntimeError):
    """An optimistic-concurrency (Lance manifest version) write conflict that the
    caller asked to surface instead of retry.

    omnigraph serializes local writes with our advisory flock, but on shared
    http(s)/s3 stores that lock is skipped and two writers racing the same
    manifest version make one commit fail with a "stale view" / "manifest table
    version" error. ``_execute`` normally masks that by re-running the same
    mutation — fine for an idempotent upsert, WRONG for a compare-and-swap claim
    where the retry would blindly re-apply the claim over whoever won the race.
    A caller passing ``surface_conflict=True`` gets this exception instead so it
    can re-read and decide (see ``task_claim``)."""


def _is_storage_version_mismatch(msg: str) -> bool:
    lowered = msg.lower()
    return all(marker in lowered for marker in _STORAGE_VERSION_MISMATCH_MARKERS)


def _friendly_storage_error(raw: str) -> str:
    """Strip the Rust panic's ANSI codes and backtrace boilerplate ("Location:",
    "Backtrace omitted...") from a storage-version-mismatch error, keeping just
    omnigraph's own message, and append witan's own fix for it."""
    cleaned = _ANSI_RE.sub("", raw).split("Location:")[0].strip()
    return (
        f"{cleaned}\n\n"
        "Run `witan migrate storage` to rebuild this store for the "
        "currently installed omnigraph version."
    )


class OmnigraphClient:
    """Subprocess wrapper for the omnigraph CLI."""

    def __init__(
        self,
        graph_uri: str,
        queries_dir: Path,
        token: str | None = None,
        guard: Callable[[str, dict], dict] | None = None,
    ) -> None:
        self.graph_uri = graph_uri
        self.queries_dir = queries_dir
        self.token = token
        self.guard = guard
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
        # strip alias prefixes: "p.slug" → "slug"
        return [{k.split(".", 1)[-1]: v for k, v in row.items()} for row in rows]

    def change(
        self,
        query_file: str,
        query_name: str,
        params: dict,
        *,
        surface_conflict: bool = False,
    ) -> None:
        """Run a named mutation query.

        The optional ``guard`` runs first: it may raise to reject the write, or
        return rewritten params (e.g. with secrets redacted) that are what
        actually get persisted. It sees the query name and full params, so it is
        the single point that covers every node type's write.

        ``surface_conflict``: raise :class:`OmnigraphConflict` on an
        optimistic-concurrency conflict instead of transparently retrying the
        write. Callers implementing a compare-and-swap (e.g. ``task_claim``) need
        this so a lost race is detectable rather than silently clobbered.
        """
        if self.guard is not None:
            params = self.guard(query_name, params)
        self._run(
            "mutate",
            "--query",
            str(self.queries_dir / query_file),
            query_name,
            "--params",
            json.dumps(params),
            surface_conflict=surface_conflict,
        )

    def apply_schema(self, schema_path) -> str:
        """Apply a schema file to the store (idempotent). Returns CLI stdout.

        Runs through the same per-store write lock + retry/repair as a mutation,
        so it can't race other writers and leave the store drifted.
        """
        cmd = [
            self._binary,
            "schema",
            "apply",
            "--schema",
            str(schema_path),
            self.graph_uri,
        ]
        return self._execute(cmd, "schema apply", is_write=True)

    # ── Internals ─────────────────────────────────────────────────

    def _run(self, subcommand: str, *args: str, surface_conflict: bool = False) -> str:
        quiet = ["--quiet"] if subcommand in _WRITE_SUBCOMMANDS else []
        cmd = [self._binary, subcommand, "--store", self.graph_uri, *quiet, *args]
        return self._execute(
            cmd,
            subcommand,
            is_write=subcommand in _WRITE_SUBCOMMANDS,
            surface_conflict=surface_conflict,
        )

    def _execute(
        self,
        cmd: list[str],
        label: str,
        *,
        is_write: bool,
        surface_conflict: bool = False,
    ) -> str:
        """Run an omnigraph CLI command under the write lock (for writes) with the
        retry/repair loop for optimistic-concurrency conflicts."""
        env = dict(os.environ)
        if self.token:
            env["OMNIGRAPH_SERVER_BEARER_TOKEN"] = self.token

        lock_fh = self._acquire_write_lock(is_write)
        try:
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    result = subprocess.run(
                        cmd, capture_output=True, text=True, env=env
                    )
                except OSError as exc:
                    raise RuntimeError(
                        f"omnigraph {label} could not run: {exc}"
                    ) from exc
                if result.returncode == 0:
                    return result.stdout
                err = result.stderr
                if _is_storage_version_mismatch(err):
                    raise RuntimeError(_friendly_storage_error(err)) from None
                if surface_conflict and any(m in err for m in _RETRYABLE):
                    # A compare-and-swap caller wants to lose the race, not
                    # re-apply its write over the winner. Surface immediately.
                    raise OmnigraphConflict(err.strip()) from None
                if attempt + 1 < _MAX_ATTEMPTS:
                    if any(m in err for m in _NEEDS_REPAIR):
                        self._repair(env)
                        continue
                    if any(m in err for m in _RETRYABLE):
                        time.sleep(0.05 * (attempt + 1))
                        continue
                raise RuntimeError(
                    f"omnigraph {label} failed (exit {result.returncode}):\n"
                    f"{err.strip()}"
                )
            raise AssertionError("unreachable")  # pragma: no cover
        finally:
            if lock_fh is not None:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                lock_fh.close()

    def _acquire_write_lock(self, is_write: bool):
        """Hold a per-store exclusive lock for writes (local stores)."""
        if not is_write or self.graph_uri.startswith(("http://", "https://", "s3://")):
            return None
        lock_path = Path(f"{self.graph_uri}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "w")  # noqa: SIM115 — released in _execute's finally
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    def _repair(self, env: dict) -> None:
        """Reconcile manifest/HEAD drift so the retried write can proceed."""
        subprocess.run(
            [
                self._binary,
                "repair",
                "--store",
                self.graph_uri,
                "--confirm",
                "--force",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            env=env,
        )

    @staticmethod
    def _find_binary() -> str:
        binary = shutil.which("omnigraph")
        if binary is not None:
            return binary
        # MCP servers are often launched by a desktop app or IDE extension
        # whose process doesn't inherit a shell PATH — `witan setup` always
        # installs to this fixed location, so check it directly rather than
        # relying on PATH alone.
        fallback = Path.home() / ".local" / "bin" / "omnigraph"
        if fallback.exists():
            return str(fallback)
        raise RuntimeError("omnigraph binary not found. Install via: witan setup")
