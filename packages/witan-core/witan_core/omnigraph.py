"""Shared base for the omnigraph client.

Both servers drive omnigraph the same way: store addressing that picks
``--store <uri>`` for local/s3 stores and ``--server <url> --graph <id>``
for a deployed omnigraph-server (omnigraph 0.8.1 rejects an http(s) ``--store``),
a per-store advisory write lock for local stores, a retry/repair loop for
optimistic-concurrency drift, self-backoff for the deployed omnigraph-server's
per-actor admission cap and for the window in which it is restarting, and named
read/mutate queries. That LOCAL-vs-REMOTE-generic surface lives here.

TWO TRANSPORTS, ONE POLICY

Calls reach omnigraph either by running the ``omnigraph`` CLI as a subprocess or,
for ``query``/``mutate`` against a deployed server, over a pooled HTTP connection
(:mod:`witan_core.omnigraph_http`) — the subprocess is 77-81% of every remote
read on a compacted store. The HTTP path is NOT a general replacement: omnigraph
0.8.1 serves only ``query``, ``mutate`` and ``graphs`` over HTTP, so ``load``,
``branch``, ``commit``, ``optimize``, ``cleanup``, ``schema apply`` and
``repair`` all still shell out, as does any client that injects extra CLI args
(witan-code's ``--branch`` views). ``_http_transport`` is the single place that
decides.

What must not fork is the POLICY. Both transports produce an ``_AttemptResult``
carrying one of the classification kinds, and ``_with_retry_policy`` runs the
same loop over either — so a restarting server, an admission cap, a
compare-and-swap conflict or a store needing repair behaves identically however
the call was made. Only the CLASSIFICATION differs: stderr text for the
subprocess, HTTP status for the transport.

Each server subclasses to add its own tail:

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
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from witan_core import omnigraph_http as _http

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

# Escape hatch for the pooled HTTP transport (see ``_http_transport``). Set to
# "0"/"false"/"no" to force every call back onto the CLI subprocess.
#
# This is on the path of every read against the deployed service, and the CLI
# path stays fully maintained beneath it (it is still the only way to reach
# `load`, `branch`, `optimize`, …), so keeping a one-variable revert costs
# nothing and means a transport-specific problem in production is a Deployment
# env change rather than an image rebuild.
HTTP_TRANSPORT_ENV_VAR = "WITAN_OMNIGRAPH_HTTP"
_FALSEY = {"0", "false", "no", "off"}

# A deployed omnigraph-server is reached over http(s); local files and s3://
# roots are opened directly. Only http(s) needs the `--server`/`--graph`
# addressing split — s3:// keeps `--store` (omnigraph opens it directly).
_SERVER_SCHEMES = ("http://", "https://")
# omnigraph graph ids: letters, digits, hyphens; 1-64 chars. NO underscores
# (the engine reserves them) and no path separators — see the naming decision
# in memory pf-decision-cluster-graph-names-track-package.
_GRAPH_ID_RE = re.compile(r"^[a-zA-Z0-9-]{1,64}$")


# ── Mutation batching ────────────────────────────────────────────────────────
#
# The commit unit is the `omnigraph mutate` INVOCATION, not the statement: a
# query body may hold many statements and they all land in one Lance version.
# That is the only batching the query language offers — there is no row-array
# parameter (`$rows: [Memory]` is a parse error, "expected base_type") and no
# loop form (`for $r in $rows` is "expected query_body"), so statement count is
# fixed when the query text is written. A batch whose arity varies per call —
# a memory with 0..N tags — therefore cannot be a static entry in a `.gq` file,
# and has to be assembled here and passed inline with `-e`.
#
# Measured on omnigraph 0.8.1, 20 Topic rows into a fresh store: 20 separate
# mutates cost 1.85s / 20 manifest versions / 25 fragments; one 20-statement
# mutate cost 0.095s / 1 manifest version / 6 fragments. Edges may reference
# nodes inserted earlier in the SAME body (endpoint validation resolves against
# the in-flight statements), which is what lets a node and its edges share a
# commit rather than needing one each.
#
# Rather than restating each mutation's field list in Python — duplicating what
# `mutations.gq` already says, and dropping out of `omnigraph lint`'s reach —
# the bodies are spliced out of the .gq source and concatenated. Each step's
# parameters are renamed with a per-step prefix so two steps that both take
# `$slug` don't collide.
_QUERY_DECL_RE = re.compile(r"\bquery\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)
_PARAM_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _match_delimited(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index just past the ``open_ch`` at ``start``'s matching ``close_ch``.

    A depth counter rather than a regex because both the parameter list and the
    body nest: `insert Memory { … }` puts braces inside the query's own braces.
    String literals are skipped so a brace or paren inside one is not counted.
    """
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError(f"unbalanced {open_ch}{close_ch} in query source")


def parse_query(source: str, name: str) -> tuple[str, str]:
    """Return ``(param_decls, body)`` for the named query in ``.gq`` ``source``.

    ``param_decls`` is the text between the parentheses and ``body`` the text
    between the braces, both verbatim so the caller can splice them. Comments
    are stripped first: a `//` line inside a body would otherwise swallow the
    statements that follow it once everything is joined onto fewer lines.
    """
    source = _LINE_COMMENT_RE.sub("", source)
    for match in _QUERY_DECL_RE.finditer(source):
        if match.group(1) != name:
            continue
        params_end = _match_delimited(source, match.end() - 1, "(", ")")
        params = source[match.end() : params_end - 1]
        body_start = source.index("{", params_end)
        body_end = _match_delimited(source, body_start, "{", "}")
        return params.strip(), source[body_start + 1 : body_end - 1].strip()
    raise KeyError(f"query {name!r} not found in source")


def compose_batch(
    steps: list[tuple[str, str, dict]],
    read_source: Callable[[str], str],
    *,
    query_name: str = "witan_batch",
) -> tuple[str, dict]:
    """Splice ``steps`` into one multi-statement GQ query.

    Each step is ``(query_file, query_name, params)``. Returns the GQ source and
    the merged parameter dict, ready for ``mutate -e <gq> <query_name>``.

    Parameters are prefixed with the step's 0-based position — `$slug` in the
    FIRST step becomes `$s0_slug`, in the second `$s1_slug` — so steps reusing a
    name stay independent.

    Every declared parameter must be supplied, exactly as ``change`` already
    requires — optional fields are passed explicitly as ``None``. A declaration
    cannot simply be dropped when a caller omits it, because the body still
    references it; checking here names the missing parameter instead of leaving
    omnigraph to report an unbound variable in generated source.
    """
    decls: list[str] = []
    bodies: list[str] = []
    merged: dict = {}
    for index, (query_file, name, params) in enumerate(steps):
        prefix = f"s{index}_"
        raw_decls, body = parse_query(read_source(query_file), name)
        declared = {
            m.group(1)
            for d in _split_decls(raw_decls)
            if (m := _PARAM_REF_RE.search(d))
        }
        if missing := declared - set(params):
            raise KeyError(f"{name} missing parameter(s): {', '.join(sorted(missing))}")

        def rename(match: re.Match, prefix: str = prefix) -> str:
            return f"${prefix}{match.group(1)}"

        decls += [_PARAM_REF_RE.sub(rename, d) for d in _split_decls(raw_decls)]
        bodies.append(_PARAM_REF_RE.sub(rename, body))
        merged.update({f"{prefix}{k}": v for k, v in params.items()})
    joined = "\n".join(f"    {line}" for b in bodies for line in b.splitlines())
    return f"query {query_name}({', '.join(decls)}) {{\n{joined}\n}}\n", merged


def extract_query(source: str, name: str) -> str:
    """Return the named query from ``.gq`` ``source`` as a standalone query.

    The HTTP API takes the query TEXT inline (``{"query": …, "params": …}``) and
    has no field for a query name, where the CLI takes ``--query <file> <name>``
    and picks. So the body must contain exactly the one query being run — not the
    whole file — or which query executes would depend on how the server resolves
    a multi-query source, which is not a behaviour worth depending on.

    Rebuilt from :func:`parse_query`'s parts rather than sliced out verbatim so
    both transports go through the same parser: if the splice is ever wrong, it
    is wrong for ``change_many`` too and the existing batch tests catch it.
    """
    decls, body = parse_query(source, name)
    return f"query {name}({decls}) {{\n{body}\n}}\n"


# ── Attempt classification ───────────────────────────────────────────────────
#
# One retry/backoff policy serves both transports (see ``_with_retry_policy``),
# so both must describe a failure in the same vocabulary. The subprocess path
# classifies by matching the CLI's stderr text — the only signal it has — and the
# HTTP path classifies by status code and exception type, which is strictly
# better information for the same conditions. Keeping the POLICY in one place and
# varying only the CLASSIFICATION is what stops the two paths from drifting into
# behaving differently under load.


class _AttemptResult(NamedTuple):
    kind: str
    body: str = ""
    error: str = ""
    returncode: int | None = None


def _classify_cli_error(stderr: str) -> str:
    """Map an omnigraph CLI failure's stderr onto the shared kinds."""
    lowered = stderr.lower()
    if any(m in lowered for m in _UNAVAILABLE_MARKERS):
        return _http.UNAVAILABLE
    if any(m in lowered for m in _ADMISSION_CAP_MARKERS):
        return _http.ADMISSION_CAP
    if any(m in lowered for m in _NEEDS_REPAIR):
        return _http.NEEDS_REPAIR
    if any(m in lowered for m in _RETRYABLE):
        return _http.RETRYABLE
    return _http.FATAL


# Transports are PROCESS-wide, keyed by server url, because clients are not:
# witan constructs a fresh OmnigraphClient per request so per-actor tokens cannot
# race through shared mutable state (ADR-0004). A transport owned by the client
# would therefore be built and thrown away per call, every call would open a new
# connection, and the keep-alive reuse this whole module exists for would never
# happen — the change would measure as no faster than the subprocess it replaced.
#
# Sharing them is safe precisely because the transport holds no per-actor state:
# the token is passed in per call, and the connections live in thread-locals
# inside it. What is shared is a socket to a host, which is what a pool is.
_TRANSPORTS: dict[str, _http.PooledTransport] = {}
_TRANSPORTS_LOCK = threading.Lock()


def _shared_transport(server_url: str) -> _http.PooledTransport:
    with _TRANSPORTS_LOCK:
        transport = _TRANSPORTS.get(server_url)
        if transport is None:
            transport = _http.PooledTransport(server_url)
            _TRANSPORTS[server_url] = transport
        return transport


# The .gq sources are packaged data and effectively immutable at runtime, but a
# `stat` per call is cheap next to a 5ms request and keeps an edit picked up
# during development — the CLI re-read the file on every invocation, so caching
# without an mtime check would be the one behaviour this change silently loses.
_QUERY_TEXT_CACHE: dict[tuple[str, str, float], str] = {}


def _cached_query_text(path: Path, query_name: str) -> str:
    key = (str(path), query_name, path.stat().st_mtime)
    text = _QUERY_TEXT_CACHE.get(key)
    if text is None:
        text = extract_query(path.read_text(), query_name)
        _QUERY_TEXT_CACHE[key] = text
    return text


def _split_decls(raw: str) -> list[str]:
    """Split a parameter-declaration list on top-level commas.

    Not a plain ``split(",")``: a list type (`$tags: [String]?`) has no comma
    inside today, but a defaulted or nested declaration would, and getting this
    wrong silently mangles a parameter name.
    """
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in raw:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


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
        connect_retry: bool = True,
    ) -> None:
        self.graph_uri = graph_uri
        self.queries_dir = queries_dir
        self.token = token
        self.guard = guard
        # Whether a remote call rides out an unreachable server for the full
        # _UNAVAILABLE_MAX_WAIT budget. On by default: that budget exists so an
        # ordinary call survives a server restart instead of failing the whole
        # run. Turn it OFF for a call whose answer *degrades* rather than fails
        # — a listing, an existence check — where waiting 150s to report "no"
        # is strictly worse than reporting it now.
        self.connect_retry = connect_retry
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
        transport = self._http_transport()
        if transport is not None:
            # The server answers with the same JSON body the CLI prints on
            # stdout — `{rows, columns, row_count}`, alias-prefixed column keys
            # and all — so the parsing below is shared verbatim rather than
            # forked per transport.
            result = self._http_execute(
                transport,
                "query",
                self._query_source(query_file, query_name),
                params,
                "query",
            )
        else:
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
        transport = self._http_transport()
        if transport is not None:
            self._http_execute(
                transport,
                "mutate",
                self._query_source(query_file, query_name),
                params,
                "mutate",
                surface_conflict=surface_conflict,
            )
            return
        self._run(
            "mutate",
            "--query",
            str(self.queries_dir / query_file),
            query_name,
            "--params",
            json.dumps(params),
            surface_conflict=surface_conflict,
        )

    def change_many(
        self,
        steps: list[tuple[str, str, dict]],
        *,
        surface_conflict: bool = False,
        chunk_size: int | None = None,
    ) -> None:
        """Run several named mutations as ONE commit.

        ``steps`` is ``[(query_file, query_name, params), …]``, the same triples
        ``change`` takes one at a time. They are spliced into a single
        multi-statement query (see ``compose_batch``) and run as one ``mutate``,
        so N rows cost one Lance version instead of N — which is both ~19x
        faster and the difference between a store that fragments and one that
        does not.

        ORDER IS PRESERVED AND SIGNIFICANT: an edge statement may reference a
        node inserted by an earlier step, so a node must come before its edges.

        A query is deliberately constructive OR destructive, never both — the
        engine rejects a body mixing inserts/updates with deletes. Deletes batch
        freely with other deletes; just keep them in their own call.

        NOT for a compare-and-swap. A batch commits or fails whole, so a
        conflict cannot be attributed to one step — ``task_claim`` and anything
        else that needs to detect losing a race must stay on ``change``.

        ``chunk_size`` splits the work into that many statements per commit and
        must be >= 1.
        The composed query is passed as a single argv element, so a caller with
        an unbounded number of steps (a repo reindex, a store-wide backfill)
        must cap it or eventually exceed ARG_MAX / a server payload limit. It is
        opt-in because it TRADES AWAY ATOMICITY: chunks commit independently and
        a failure part-way leaves the earlier ones applied.

        A single step is passed straight through to ``change`` rather than
        wrapped: the composed form would be equivalent, but the direct path
        keeps the named query in the error message and skips the splice.
        """
        if not steps:
            return
        if chunk_size is not None and chunk_size < 1:
            # Not a redundant type check: `range(0, n, -1)` is EMPTY, so a
            # negative size would skip the loop, return normally, and silently
            # drop every write. Zero raises, but as an opaque "range() arg 3
            # must not be zero" far from the caller that chose the value.
            raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
        if chunk_size is not None and len(steps) > chunk_size:
            for start in range(0, len(steps), chunk_size):
                self.change_many(
                    steps[start : start + chunk_size],
                    surface_conflict=surface_conflict,
                )
            return
        if len(steps) == 1:
            query_file, name, params = steps[0]
            self.change(query_file, name, params, surface_conflict=surface_conflict)
            return
        if self.guard is not None:
            steps = [(f, n, self.guard(n, p)) for f, n, p in steps]
        # Read each .gq once, not once per step: the steps of a batch nearly
        # always come from the same file (a memory and all its tags are
        # mutations.gq), and re-reading it per step would put avoidable I/O on
        # the path whose whole purpose is to be faster.
        sources: dict[str, str] = {}

        def read_source(query_file: str) -> str:
            if query_file not in sources:
                sources[query_file] = (self.queries_dir / query_file).read_text()
            return sources[query_file]

        source, params = compose_batch(steps, read_source)
        transport = self._http_transport()
        if transport is not None:
            # compose_batch already produces a single standalone query, which is
            # exactly the inline form the HTTP body wants — no extraction step.
            self._http_execute(
                transport,
                "mutate",
                source,
                params,
                "mutate",
                surface_conflict=surface_conflict,
            )
            return
        self._run(
            "mutate",
            "-e",
            source,
            "witan_batch",
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
        (e.g. witan-code injects ``--branch``).

        Returning anything non-empty also opts the client OUT of the pooled HTTP
        transport, because these args have no expressed HTTP equivalent and
        dropping them would silently retarget the call — see
        ``_http_transport``.
        """
        return []

    # ── Pooled HTTP transport (reads/writes against a deployed server) ──

    def _http_transport(self) -> _http.PooledTransport | None:
        """The pooled transport for this store, or ``None`` to use the CLI.

        Three conditions have to hold, and the third is the interesting one:

        1. The store is a deployed omnigraph-server (``http(s)://``). A local
           path or an ``s3://`` root has no server to talk to.
        2. The escape hatch (:data:`HTTP_TRANSPORT_ENV_VAR`) is not switched off.
        3. **This client injects no extra CLI args for the verb.** The only
           subclass that does is witan-code, which adds ``--branch`` for its
           per-user/per-branch code-graph views. omnigraph 0.8.1's HTTP API
           documents no branch selector on the request body — the response
           carries a ``target: {branch, snapshot}``, which hints one may exist,
           but hinted is not verified. Routing a branched call over HTTP without
           it would silently execute against ``main``: a WIP reindex landing in
           the shared graph, which is the exact failure branch views exist to
           prevent. So branched clients stay on the CLI until the request-side
           selector is confirmed, and the check is written against
           ``_extra_args`` rather than against ``branch`` so any FUTURE subclass
           arg is caught by the same guard instead of quietly being dropped.

        Built lazily and cached: constructing it parses a URL and allocates a
        ``threading.local``, and witan builds a fresh client per request to keep
        per-actor tokens from racing (ADR-0004). The pooled CONNECTIONS live in
        thread-locals inside the transport, so they are what actually needs to
        outlive the client — see ``_TRANSPORTS``.
        """
        if not self.is_remote:
            return None
        if os.environ.get(HTTP_TRANSPORT_ENV_VAR, "").strip().lower() in _FALSEY:
            return None
        if self._extra_args("query") or self._extra_args("mutate"):
            return None
        return _shared_transport(self.server_url)

    def _resolve_token(self) -> str | None:
        """The bearer token to present over HTTP.

        Mirrors what the CLI subprocess would resolve, and the fallback is the
        load-bearing half: a remote client with no configured token deliberately
        inherits an ambient ``OMNIGRAPH_BEARER_TOKEN`` (the CLI's own documented
        last resort, and a supported way to drive a remote graph). Sending only
        ``self.token`` would silently drop that and turn a working setup into
        unauthenticated 401s — the same class of failure as the
        wrong-variable-name bug this env var is named for.

        Read per call rather than cached at construction so the two transports
        resolve it at the same moment, and so a test or a process that exports
        the variable late behaves the same either way.
        """
        return self.token or os.environ.get(BEARER_TOKEN_ENV_VAR) or None

    def _query_source(self, query_file: str, query_name: str) -> str:
        """The named query's text, for the HTTP body (which takes it inline)."""
        path = self.queries_dir / query_file
        return _cached_query_text(path, query_name)

    def _http_execute(
        self,
        transport: _http.PooledTransport,
        verb: str,
        source: str,
        params: dict,
        label: str,
        *,
        surface_conflict: bool = False,
    ) -> str:
        """Run one query/mutate over HTTP under the shared retry policy."""
        is_write = verb == "mutate"
        call = transport.mutate if is_write else transport.query

        token = self._resolve_token()

        def attempt() -> _AttemptResult:
            outcome = call(self.graph_id, source, params, token)
            return _AttemptResult(outcome.kind, body=outcome.body, error=outcome.error)

        return self._with_retry_policy(
            attempt,
            label,
            # Only consulted if a repair is needed, which still shells out —
            # `repair` has no HTTP form, so the CLI env is what it runs with.
            env=self._subprocess_env(),
            is_write=is_write,
            surface_conflict=surface_conflict,
        )

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
        env = self._subprocess_env()

        def attempt() -> _AttemptResult:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, env=env)
            except OSError as exc:
                raise RuntimeError(f"omnigraph {label} could not run: {exc}") from exc
            if result.returncode == 0:
                return _AttemptResult(_http.OK, body=result.stdout, returncode=0)
            return _AttemptResult(
                _classify_cli_error(result.stderr),
                error=result.stderr,
                returncode=result.returncode,
            )

        return self._with_retry_policy(
            attempt,
            label,
            env=env,
            is_write=is_write,
            surface_conflict=surface_conflict,
        )

    def _subprocess_env(self) -> dict:
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
        return env

    def _with_retry_policy(
        self,
        attempt_once: Callable[[], _AttemptResult],
        label: str,
        *,
        env: dict,
        is_write: bool,
        surface_conflict: bool = False,
    ) -> str:
        """The retry/backoff policy, shared by both transports.

        ``attempt_once`` performs ONE call and classifies its outcome; everything
        about *whether to call again* lives here. Both the CLI subprocess and the
        pooled HTTP transport feed this same loop, so a restarting server, an
        admission cap, or an optimistic-concurrency conflict is handled
        identically no matter how the call was made.
        """
        lock_fh = self._acquire_write_lock(is_write)
        try:
            attempt = 0
            admission_cap_attempt = 0
            unavailable_attempt = 0
            unavailable_started: float | None = None
            while True:
                result = attempt_once()
                if result.kind == _http.OK:
                    return result.body
                err = result.error
                kind = result.kind
                if self._STORAGE_MISMATCH_HINT and _is_storage_version_mismatch(err):
                    raise RuntimeError(
                        _friendly_storage_error(err, self._STORAGE_MISMATCH_HINT)
                    ) from None
                if kind == _http.UNAVAILABLE and not (
                    self.is_remote and self.connect_retry
                ):
                    # A connect failure against a LOCAL store is not a restarting
                    # server, and a caller that opted out of the wait wants an
                    # answer now. Either way it stops being a retryable
                    # condition and falls through to the generic failure below.
                    kind = _http.FATAL
                if kind == _http.UNAVAILABLE:
                    # Its own budget, like the admission cap below: this is a
                    # gap in the server's availability, not a conflict over the
                    # graph, so it neither consumes _MAX_ATTEMPTS nor honours
                    # surface_conflict (there is no conflict to surface).
                    #
                    # Reaching here means re-running is known safe. For a WRITE
                    # that is because the request provably never left this
                    # process (the CLI's "tcp connect error", or a failure
                    # during the transport's explicit `connect()`); a write
                    # whose fate is ambiguous is classified FATAL by both
                    # transports and never arrives here. Reads may also arrive
                    # here from a mid-flight failure, which is fine precisely
                    # because repeating a query changes nothing.
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
                if kind == _http.ADMISSION_CAP:
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
                if surface_conflict and kind == _http.RETRYABLE:
                    # A compare-and-swap caller wants to lose the race, not
                    # re-apply its write over the winner. Surface immediately.
                    raise OmnigraphConflict(err.strip()) from None
                attempt += 1
                if attempt < _MAX_ATTEMPTS:
                    if kind == _http.NEEDS_REPAIR:
                        self._repair(env)
                        continue
                    if kind == _http.RETRYABLE:
                        time.sleep(0.05 * attempt)
                        continue
                # `exit N` only makes sense for a subprocess; an HTTP attempt
                # carries no returncode and says so by omitting it, rather than
                # inventing one that would read as a CLI exit status.
                exited = (
                    "" if result.returncode is None else f" (exit {result.returncode})"
                )
                raise RuntimeError(f"omnigraph {label} failed{exited}:\n{err.strip()}")
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
