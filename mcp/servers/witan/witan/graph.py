import fcntl
import json
import os
import random
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

# omnigraph-server (remote http(s)/s3 stores) enforces a hard per-actor
# admission cap — both an in-flight *count* (default 16, configurable via
# OMNIGRAPH_PER_ACTOR_INFLIGHT_MAX) and a concurrent *byte* budget
# (OMNIGRAPH_PER_ACTOR_BYTES_MAX) — and rejects excess concurrent writes
# outright (HTTP 429) rather than queuing them. Confirmed against the
# omnigraph source (crates/omnigraph-server/src/workload.rs): both reject
# reasons map to 429 with a constant `Retry-After: 60` header, but the CLI's
# error path only surfaces the JSON body's message text, not headers — so
# this can't read Retry-After and backs off on its own, much shorter,
# schedule (60s is a conservative RFC ceiling, not a measured value; in
# practice the cap clears as soon as any of the actor's other in-flight
# writes complete). This is unrelated to the local Lance drift/OCC conflicts
# above (server mode doesn't see those), so it gets its own budget —
# independent of _MAX_ATTEMPTS and of surface_conflict (it isn't a
# compare-and-swap race, just admission control).
_ADMISSION_CAP_MARKERS = ("in-flight count cap", "byte budget exceeded")
_ADMISSION_CAP_MAX_ATTEMPTS = 6
_ADMISSION_CAP_BASE_DELAY = 0.25
_ADMISSION_CAP_MAX_DELAY = 4.0


def _admission_cap_backoff(attempt: int) -> float:
    """Exponential backoff with jitter — plain exponential backoff makes
    concurrent retries from the same burst (the actual trigger case here)
    retry in lockstep and re-collide on the cap; jitter breaks that up."""
    delay = _ADMISSION_CAP_BASE_DELAY * (2 ** (attempt - 1))
    jitter = random.uniform(0, 0.1 * delay)
    return min(delay + jitter, _ADMISSION_CAP_MAX_DELAY)


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

    def optimize(self) -> str:
        """Compact small Lance fragments across every table (non-destructive).

        Every witan write appends a new tiny Lance fragment + manifest version;
        an un-compacted store bloats until *opening* it dominates query latency
        (a fixed per-query cost regardless of rows). ``omnigraph optimize``
        collapses the fragments. It takes the store's write lock and can run for
        tens of seconds on a bloated store, so callers must throttle it and keep
        it off the prompt path (see ``witan.maintenance``). Runs under the same
        per-store write lock + retry/repair loop as a mutation.
        """
        cmd = [self._binary, "optimize", "--store", self.graph_uri, "--quiet"]
        return self._execute(cmd, "optimize", is_write=True)

    def cleanup(self, *, keep: int | None = None, older_than: str | None = None) -> str:
        """Reclaim disk by removing old Lance versions (**destructive**).

        ``optimize`` compacts fragments but leaves old versions behind, so disk
        stays large until they are GC'd. ``cleanup`` removes them, keeping the
        most recent ``keep`` versions per table and/or those newer than
        ``older_than`` (a Go-style duration like ``7d``). At least one bound must
        be given (omnigraph requires it). ``--confirm`` is passed so it actually
        runs. Local-store only in practice; runs under the write lock.
        """
        if keep is None and older_than is None:
            raise ValueError("cleanup requires keep and/or older_than")
        cmd = [
            self._binary,
            "cleanup",
            "--store",
            self.graph_uri,
            "--confirm",
            "--quiet",
        ]
        if keep is not None:
            cmd += ["--keep", str(keep)]
        if older_than is not None:
            cmd += ["--older-than", older_than]
        return self._execute(cmd, "cleanup", is_write=True)

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
            attempt = 0
            admission_cap_attempt = 0
            while True:
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
                err_lower = err.lower()
                if _is_storage_version_mismatch(err):
                    raise RuntimeError(_friendly_storage_error(err)) from None
                if any(m in err_lower for m in _ADMISSION_CAP_MARKERS):
                    # Independent budget/backoff from the drift retries below —
                    # doesn't consume _MAX_ATTEMPTS and ignores surface_conflict.
                    admission_cap_attempt += 1
                    if admission_cap_attempt < _ADMISSION_CAP_MAX_ATTEMPTS:
                        time.sleep(_admission_cap_backoff(admission_cap_attempt))
                        continue
                    raise RuntimeError(
                        f"omnigraph {label} failed after "
                        f"{_ADMISSION_CAP_MAX_ATTEMPTS} attempts (actor "
                        f"admission cap exceeded):\n{err.strip()}"
                    )
                if surface_conflict and any(m in err_lower for m in _RETRYABLE):
                    # A compare-and-swap caller wants to lose the race, not
                    # re-apply its write over the winner. Surface immediately.
                    raise OmnigraphConflict(err.strip()) from None
                attempt += 1
                if attempt < _MAX_ATTEMPTS:
                    if any(m in err_lower for m in _NEEDS_REPAIR):
                        self._repair(env)
                        continue
                    if any(m in err_lower for m in _RETRYABLE):
                        time.sleep(0.05 * attempt)
                        continue
                raise RuntimeError(
                    f"omnigraph {label} failed (exit {result.returncode}):\n"
                    f"{err.strip()}"
                )
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
