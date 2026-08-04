"""Shared base for the omnigraph CLI subprocess wrapper.

Both servers drive the ``omnigraph`` binary the same way: store addressing that
picks ``--store <uri>`` for local/s3 stores and ``--server <url> --graph <id>``
for a deployed omnigraph-server (omnigraph 0.8.1 rejects an http(s) ``--store``),
a per-store advisory write lock for local stores, a retry/repair loop for
optimistic-concurrency drift, self-backoff for the deployed omnigraph-server's
per-actor admission cap and for the window in which it is restarting, and named
read/mutate queries. That LOCAL-vs-REMOTE-generic surface lives here; each
server subclasses to add its own tail:

- ``witan`` adds a write ``guard`` + ``surface_conflict`` (CAS task claims),
  ``apply_schema``, and the storage-version friendly-error hint.
- ``witan_code`` adds omnigraph *branch* support (``_extra_args`` override,
  ``ensure_branch``/…) and bulk ``load``.

Subclass knobs:
- ``_SETUP_HINT`` — the install command named in the "binary not found" error.
- ``_STORAGE_MISMATCH_HINT`` — remediation text for a storage-version mismatch;
  ``None`` (the default) leaves that error on the generic failure path.
- ``_extra_args(subcommand)`` — extra CLI args injected into every ``_run`` (e.g.
  ``--branch``); returns ``[]`` by default.

This module is stdlib-only but imports ``fcntl`` (POSIX advisory locks), so it is
NOT re-exported from ``witan_core``'s package root — import it as
``witan_core.omnigraph``.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import re
import shutil
import subprocess
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

# omnigraph local stores use optimistic concurrency (Lance manifest versions) and
# are NOT safe for concurrent writers. We serialize writes with a per-store
# advisory lock (prevention) and, as a safety net, retry transient "stale view"
# conflicts and `omnigraph repair` stores already in the drifted state.
_WRITE_SUBCOMMANDS = {"mutate", "load", "optimize", "cleanup"}
_RETRYABLE = ("stale view", "manifest table version", "refresh and retry")
_NEEDS_REPAIR = ("ahead of manifest", "omnigraph repair")
_MAX_ATTEMPTS = 8

# omnigraph uses strict single-version storage: a release that bumps the
# internal schema version refuses to open graphs an older binary wrote,
# raising exactly this pair of substrings wrapped in an unhelpful Rust panic +
# backtrace. Not retryable/repairable.
_STORAGE_VERSION_MISMATCH_MARKERS = ("stamped at internal schema", "reads only")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# omnigraph-server (remote http(s)/s3 stores) enforces a hard per-actor
# admission cap — both an in-flight *count* (default 16) and a concurrent
# *byte* budget — and rejects excess concurrent writes outright (HTTP 429)
# rather than queuing them. The CLI's error path only surfaces the JSON body's
# message text, not the `Retry-After: 60` header, so this can't read it and
# backs off on its own, much shorter, schedule. Independent of _MAX_ATTEMPTS
# and of surface_conflict (it isn't a compare-and-swap race, just admission
# control). Lives in the shared base because witan-code also writes to the
# deployed omnigraph-server.
_ADMISSION_CAP_MARKERS = ("in-flight count cap", "byte budget exceeded")
_ADMISSION_CAP_MAX_ATTEMPTS = 6
_ADMISSION_CAP_BASE_DELAY = 0.25
_ADMISSION_CAP_MAX_DELAY = 4.0

# The deployed omnigraph-server goes away entirely for a stretch on every
# restart, and restarts are routine: adding a user to the actor-tokens map is
# a restart, because the server hashes OMNIGRAPH_SERVER_BEARER_TOKENS_FILE once
# at boot and never re-reads it (upstream has no SIGHUP/admin reload — see
# ol-infrastructure task tk-omnigraph-server-actor-token-hot-reload-...-0e878a,
# which resolves that by having the Vault Secrets Operator rolling-restart the
# Deployment whenever the token map changes). The data tier is deliberately
# replicas=1 + strategy=Recreate (its storage is strict-single-version, so two
# binaries must never hold the same S3 store), so that restart is a hard gap
# with no endpoint at all — not a rolling one. Without this, every concurrent
# MCP tool call in that window fails outright.
#
# ONLY connection-*establishment* failures are retried. Those provably never
# reached the server, so re-running is safe for a mutate as much as a read. A
# mid-flight failure (connection reset, request timeout, 5xx) is deliberately
# NOT matched: the write may already have committed, and silently re-applying
# it is worse than surfacing the error — the same reasoning that makes
# `surface_conflict` exist.
# The budget is a WALL-CLOCK DEADLINE, not an attempt count. It was originally
# an attempt count, and that framing is what got it wrong: 12 attempts of
# capped exponential backoff sums to ~42s, which reads fine until you ask the
# only question that matters — "is it longer than a restart?" It was not.
#
# Measured against the CI deployment (2026-08-03, two independent restarts of
# the real Deployment, one triggered by adding a token and one by removing it):
#
#     old container killed -> new pod scheduled    30s   terminationGracePeriod
#     scheduled -> container started                1s
#     container started -> Ready                21-30s   boot + readiness probe
#     TOTAL UNREACHABLE                         52s, 61s
#
# The 30s termination is the full grace period every time, exactly, because the
# server does not exit on SIGTERM and is SIGKILLed at the deadline. The boot
# half is the binary opening its S3-backed graphs — the port is still unbound
# ~19s in. Neither half is going to get dramatically faster, so the deadline is
# set well above the observed worst case rather than hugged to it: a slower
# node, a cold image pull, or a larger graph all push the real number up.
_UNAVAILABLE_MARKERS = ("tcp connect error", "dns error")
_UNAVAILABLE_MAX_WAIT = 150.0
_UNAVAILABLE_BASE_DELAY = 0.5
_UNAVAILABLE_MAX_DELAY = 10.0

# How a bearer token reaches the omnigraph CLI. Per its token-resolution order
# (docs/user/cli/reference.md): a server-name-specific `OMNIGRAPH_TOKEN_<NAME>`,
# then a `[<name>]` section in ~/.omnigraph/credentials, then THIS as the
# default fallback for any server. No subcommand takes a token flag — only
# `omnigraph login` does — so the env fallback is the only per-invocation form.
#
# That matters: witan resolves a DIFFERENT per-actor token per request
# (ADR-0004), and a credentials file is process-global state that concurrent
# requests for two actors would race. An env var set on each subprocess does
# not, which is why this stays a per-call `env` rather than a login at startup.
#
# NOT to be confused with the SERVER-side `OMNIGRAPH_SERVER_BEARER_TOKENS_FILE`
# (plural, a file: the {actor_id: token} map omnigraph-server boots from). This
# was previously spelled `OMNIGRAPH_SERVER_BEARER_TOKEN`, derived from that name
# by analogy — a variable the CLI has never read, which left every remote call
# from both servers unauthenticated and crash-looped the deployed migration Job.
BEARER_TOKEN_ENV_VAR = "OMNIGRAPH_BEARER_TOKEN"

# A deployed omnigraph-server is reached over http(s); local files and s3://
# roots are opened directly. Only http(s) needs the `--server`/`--graph`
# addressing split — s3:// keeps `--store` (omnigraph opens it directly).
_SERVER_SCHEMES = ("http://", "https://")
# omnigraph graph ids: letters, digits, hyphens; 1-64 chars. NO underscores
# (the engine reserves them) and no path separators — see the naming decision
# in memory pf-decision-cluster-graph-names-track-package.
_GRAPH_ID_RE = re.compile(r"^[a-zA-Z0-9-]{1,64}$")


def schema_stamp_path(store: Path) -> Path:
    """Sidecar recording the schema file's mtime as of the last successful apply."""
    return store.parent / f"{store.name}.schema_mtime"


def schema_apply(binary: str, schema_file: Path, store: Path) -> bool:
    """``omnigraph schema apply`` against a local store. Returns whether it worked.

    Deliberately no ``check=True``. Both callers run this on a path that must
    not be able to take down a working store's process — witan's
    ``_ensure_graph`` runs at import time, so a raise here fails ``witan serve``
    at startup rather than degrading. The stamp is only written on success, so
    a failed apply is simply retried on the next call.

    A non-zero exit is not the only way this fails: ``subprocess.run`` itself
    raises ``OSError`` when the binary is missing or not executable, which
    would escape the same "cannot raise" path that dropping ``check=True``
    exists to protect. Both become ``False``.
    """
    try:
        res = subprocess.run(
            [binary, "schema", "apply", "--schema", str(schema_file), str(store)],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    if res.returncode != 0:
        return False
    try:
        schema_stamp_path(store).write_text(str(schema_file.stat().st_mtime))
    except OSError:
        # A read-only or full parent directory just means the next call
        # re-applies. `schema apply` is idempotent, so that is the correct
        # failure direction — never let stamping fail the apply itself.
        pass
    return True


def schema_apply_if_changed(binary: str, schema_file: Path, store: Path) -> bool:
    """Re-apply the schema to an existing store, but only if the file changed.

    This is what picks up ADDITIVE schema changes (new node types, new fields)
    on a store that was created by an older version. Without it, a store is
    schema-applied only at creation and silently stays one revision behind
    forever, which surfaces as a query erroring or quietly returning nothing.

    ``schema apply`` is idempotent but costs a subprocess, and callers sit on
    hot paths (a per-tool-call ensure, a per-file reindex), so the schema
    file's mtime is stamped next to the store and the apply is skipped while it
    matches. Returns ``True`` when the store is known-current.

    Neither filesystem read may raise, for the same import-time reason as
    :func:`schema_apply`, and the two failures mean different things. An
    unstattable schema file leaves nothing to apply, so it is ``False`` without
    a subprocess. An unreadable stamp only means the last-applied mtime is
    unknown — which is exactly the "might be stale" case — so it falls through
    to a re-apply rather than assuming current. Erring toward a redundant
    idempotent apply is the safe direction; erring toward skipping one silently
    leaves the store a schema revision behind, which is the bug this exists to
    fix.
    """
    stamp = schema_stamp_path(store)
    try:
        current_mtime = str(schema_file.stat().st_mtime)
    except OSError:
        return False
    try:
        if stamp.exists() and stamp.read_text().strip() == current_mtime:
            return True
    except OSError:
        pass
    return schema_apply(binary, schema_file, store)


def _split_server_uri(graph_uri: str, graph_id: str | None) -> tuple[str, str]:
    """Split an http(s) graph URI into ``(server_url, graph_id)`` for omnigraph
    0.8.1 remote addressing (``--server <url> --graph <id>``).

    omnigraph 0.8.1 rejects an http(s) ``--store``: a deployed omnigraph-server
    is reachable *only* as ``--server <scheme://host[:port]> --graph <id>`` (see
    memory pf-omnigraph-0-8-1-one-server-serves-n-graphs-remot). The server URL
    is the scheme/host/port prefix; the graph id comes from the explicit
    ``graph_id`` (preferred — the deployment sets WITAN_MEMORY_URI to a bare
    server URL) or, failing that, a ``.../graphs/<id>`` path baked into the URI.
    """
    parts = urllib.parse.urlsplit(graph_uri)
    if not parts.netloc:
        raise ValueError(
            f"remote graph URI {graph_uri!r} has no host — expected "
            "http(s)://<host>[:port] (optionally .../graphs/<id>)"
        )
    server_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    segments = [s for s in parts.path.split("/") if s]
    from_path = segments[1] if len(segments) == 2 and segments[0] == "graphs" else None
    resolved = graph_id or from_path
    if not resolved:
        raise ValueError(
            f"remote graph URI {graph_uri!r} has no graph id — pass a graph id "
            "(e.g. WITAN_MEMORY_GRAPH / WITAN_CODE_GRAPH) or encode it in the URI "
            "as .../graphs/<id>"
        )
    if not _GRAPH_ID_RE.match(resolved):
        raise ValueError(
            f"invalid omnigraph graph id {resolved!r}: must match "
            f"{_GRAPH_ID_RE.pattern} (letters, digits, hyphens; no underscores)"
        )
    return server_url, resolved


def _jittered_backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with jitter — plain exponential backoff makes
    concurrent retries from the same burst (the actual trigger case for both
    callers below) retry in lockstep and re-collide; jitter breaks that up."""
    delay = base * (2 ** (attempt - 1))
    jitter = random.uniform(0, 0.1 * delay)
    return min(delay + jitter, cap)


def _admission_cap_backoff(attempt: int) -> float:
    return _jittered_backoff(
        attempt, _ADMISSION_CAP_BASE_DELAY, _ADMISSION_CAP_MAX_DELAY
    )


def _unavailable_backoff(attempt: int) -> float:
    return _jittered_backoff(attempt, _UNAVAILABLE_BASE_DELAY, _UNAVAILABLE_MAX_DELAY)


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


def _friendly_storage_error(raw: str, hint: str) -> str:
    """Strip the Rust panic's ANSI codes and backtrace boilerplate ("Location:",
    "Backtrace omitted...") from a storage-version-mismatch error, keeping just
    omnigraph's own message, and append the caller's remediation ``hint``."""
    cleaned = _ANSI_RE.sub("", raw).split("Location:")[0].strip()
    return f"{cleaned}\n\n{hint}"


class OmnigraphClient:
    """Subprocess wrapper for the omnigraph CLI (shared base)."""

    #: Install command named in the "binary not found" error. Override per server.
    _SETUP_HINT = "the omnigraph installer (`witan setup` / `witan-code setup`)"
    #: Remediation text appended to a storage-version-mismatch error. ``None``
    #: leaves that error on the generic failure path (no friendly rewrite).
    _STORAGE_MISMATCH_HINT: str | None = None

    def __init__(
        self,
        graph_uri: str,
        queries_dir: Path,
        token: str | None = None,
        guard: Callable[[str, dict], dict] | None = None,
        graph_id: str | None = None,
    ) -> None:
        self.graph_uri = graph_uri
        self.queries_dir = queries_dir
        self.token = token
        self.guard = guard
        # http(s) stores are a remote omnigraph-server, addressed with
        # `--server <url> --graph <id>`; local paths and s3:// roots keep
        # `--store <uri>`. Resolve the split once at construction so a bad
        # graph id fails fast rather than on the first subprocess.
        self.is_remote = graph_uri.startswith(_SERVER_SCHEMES)
        if self.is_remote:
            self.server_url, self.graph_id = _split_server_uri(graph_uri, graph_id)
        else:
            self.server_url, self.graph_id = None, graph_id
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
        actually get persisted.

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

        Every write appends a new tiny Lance fragment + manifest version; an
        un-compacted store bloats until *opening* it dominates query latency.
        ``omnigraph optimize`` collapses the fragments. It takes the store's
        write lock and can run for tens of seconds, so callers throttle it and
        keep it off the hot path (see each server's ``maintenance``).
        """
        return self._run("optimize")

    def cleanup(self, *, keep: int | None = None, older_than: str | None = None) -> str:
        """Reclaim disk by removing old Lance versions (**destructive**).

        ``optimize`` compacts fragments but leaves old versions behind, so disk
        stays large until they are GC'd. ``cleanup`` removes them, keeping the
        most recent ``keep`` versions per table and/or those newer than
        ``older_than`` (a Go-style duration like ``7d``). At least one bound must
        be given (omnigraph requires it). ``--confirm`` is passed so it runs.
        """
        if keep is None and older_than is None:
            raise ValueError("cleanup requires keep and/or older_than")
        args = ["--confirm"]
        if keep is not None:
            args += ["--keep", str(keep)]
        if older_than is not None:
            args += ["--older-than", older_than]
        return self._run("cleanup", *args)

    # ── Internals ─────────────────────────────────────────────────

    def _extra_args(self, subcommand: str) -> list[str]:
        """Extra CLI args injected into every ``_run`` command (after the store
        flag, before the caller's args). Empty by default; subclasses override
        (e.g. witan-code injects ``--branch``)."""
        return []

    def _store_args(self) -> list[str]:
        """The CLI flags that address this store. A remote omnigraph-server
        (http(s)) is ``--server <url> --graph <id>`` (omnigraph 0.8.1 rejects an
        http(s) ``--store``); local paths and s3:// roots are ``--store <uri>``.
        """
        if self.is_remote:
            return ["--server", self.server_url, "--graph", self.graph_id]
        return ["--store", self.graph_uri]

    def _run(self, subcommand: str, *args: str, surface_conflict: bool = False) -> str:
        quiet = ["--quiet"] if subcommand in _WRITE_SUBCOMMANDS else []
        cmd = [
            self._binary,
            subcommand,
            *self._store_args(),
            *quiet,
            *self._extra_args(subcommand),
            *args,
        ]
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
        if not self.is_remote:
            # A local path or an s3:// root is opened directly — there is no
            # server to present a bearer token to, and s3 authenticates with AWS
            # credentials instead. `env` is a copy of os.environ, so an ambient
            # token exported for cluster use would otherwise ride along into
            # every local subprocess: propagating a secret to a process that
            # has no use for it. Strip it rather than merely not setting it.
            env.pop(BEARER_TOKEN_ENV_VAR, None)
        elif self.token:
            env[BEARER_TOKEN_ENV_VAR] = self.token
        # A remote store with no explicit token deliberately keeps whatever the
        # environment already carries: that is the CLI's own documented fallback
        # (see BEARER_TOKEN_ENV_VAR), and `export OMNIGRAPH_BEARER_TOKEN=…` with
        # no token in config is a supported way to drive it.

        lock_fh = self._acquire_write_lock(is_write)
        try:
            attempt = 0
            admission_cap_attempt = 0
            unavailable_attempt = 0
            unavailable_started: float | None = None
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
                if self._STORAGE_MISMATCH_HINT and _is_storage_version_mismatch(err):
                    raise RuntimeError(
                        _friendly_storage_error(err, self._STORAGE_MISMATCH_HINT)
                    ) from None
                if self.is_remote and any(m in err_lower for m in _UNAVAILABLE_MARKERS):
                    # Its own budget, like the admission cap below: this is a
                    # gap in the server's availability, not a conflict over the
                    # graph, so it neither consumes _MAX_ATTEMPTS nor honours
                    # surface_conflict (there is no conflict to surface — the
                    # request never left this process).
                    #
                    # Elapsed is measured from the FIRST connect failure, not
                    # from entry, so a call that spent time on unrelated drift
                    # retries still gets the full restart-length window.
                    #
                    # The final sleep is CLAMPED to the time remaining rather
                    # than skipped for overshooting, so the budget is spent to
                    # the last second and the deadline itself gets one more
                    # attempt. Raising early instead would silently shorten the
                    # window by up to _UNAVAILABLE_MAX_DELAY — the same species
                    # of "the constant does not mean what it says" bug this
                    # budget was rewritten to fix.
                    now = time.monotonic()
                    if unavailable_started is None:
                        unavailable_started = now
                    unavailable_attempt += 1
                    elapsed = now - unavailable_started
                    if elapsed < _UNAVAILABLE_MAX_WAIT:
                        time.sleep(
                            min(
                                _unavailable_backoff(unavailable_attempt),
                                _UNAVAILABLE_MAX_WAIT - elapsed,
                            )
                        )
                        continue
                    raise RuntimeError(
                        f"omnigraph {label} failed after {unavailable_attempt} "
                        f"attempts over {elapsed:.0f}s — could not connect to "
                        f"{self.server_url}:\n{err.strip()}"
                    )
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
                *self._store_args(),
                "--confirm",
                "--force",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            env=env,
        )

    @classmethod
    def _find_binary(cls) -> str:
        binary = shutil.which("omnigraph")
        if binary is not None:
            return binary
        # MCP servers are often launched by a desktop app or IDE extension
        # whose process doesn't inherit a shell PATH — the `setup` command
        # always installs to this fixed location, so check it directly rather
        # than relying on PATH alone.
        fallback = Path.home() / ".local" / "bin" / "omnigraph"
        if fallback.exists():
            return str(fallback)
        raise RuntimeError(
            f"omnigraph binary not found. Install via: {cls._SETUP_HINT}"
        )
