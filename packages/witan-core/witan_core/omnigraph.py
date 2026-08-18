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
import logging
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import NamedTuple, TypeVar

from witan_core import omnigraph_http as _http

_log = logging.getLogger(__name__)
T = TypeVar("T", int, float)

# omnigraph local stores use optimistic concurrency (Lance manifest versions) and
# are NOT safe for concurrent writers. We serialize writes with a per-store
# advisory lock (prevention) and, as a safety net, retry transient "stale view"
# conflicts and `omnigraph repair` stores already in the drifted state.
_WRITE_SUBCOMMANDS = {"mutate", "load", "optimize", "cleanup"}
# The last two markers are the REMOTE write-authority precondition — a racing
# writer whose branch head moved between prepare and commit. Over HTTP that
# arrives as a 409 and `classify_status` keys on the status, which is strictly
# better; the CLI prints the message and throws the response away, so here the
# prose is all there is. Both spellings are matched because the sentence names
# the condition twice and neither half is guaranteed to survive a reword:
#
#   write authority 'graph_head:main' changed during preparation
#   (expected 01M08E24Y…, current 01M08E27K…) — reprepare from the current
#   branch state (HTTP 409, conflict)
#
# It belongs with the local Lance conflicts rather than in its own kind because
# the remedy is identical: re-read and try again, and — for a CAS caller that
# passed `surface_conflict` — lose the race cleanly instead of re-applying the
# write over whoever won it.
_RETRYABLE = (
    "stale view",
    "manifest table version",
    "refresh and retry",
    "write authority",
    "reprepare from the current branch",
)
_NEEDS_REPAIR = ("ahead of manifest", "omnigraph repair")

# The CLI's form of the two conditions `classify_status` keys on by status.
# Prose again, for the same reason as `_RETRYABLE`: `omnigraph` prints the
# message and throws the response away, so there is no 412 and no 503 to read
# here — only what it wrote to stderr.
#
# ★ ORDER MATTERS AGAINST `_RETRYABLE`, and the two are checked in the order
# declared in `_classify_cli_error`. A precondition failure must be recognised
# BEFORE the retryable markers: it is terminal, and letting a conflict-ish word
# in the same message win would retry a write the server has told us not to
# replay. See `_http.PRECONDITION_FAILED` for why 409 and 412 are opposites.
_PRECONDITION_FAILED = ("precondition_failure", "precondition failed")

# The branch-wide write barrier. Both spellings observed in one local repro,
# 2026-08-13: "pending Load recovery operation blocks writes on branch 'main'"
# and "… blocks the synchronous write/control recovery barrier (tables
# node:ClaimRow); reopen the graph read-write before retrying". Matching the
# stem they share rather than either sentence.
_RECOVERY_REQUIRED = ("recovery required", "recovery_required")
_MAX_ATTEMPTS = 8

# omnigraph uses strict single-version storage: a release that bumps the
# internal schema version refuses to open graphs an older binary wrote,
# raising exactly this pair of substrings wrapped in an unhelpful Rust panic +
# backtrace. Not retryable/repairable.
_STORAGE_VERSION_MISMATCH_MARKERS = ("stamped at internal schema", "reads only")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# omnigraph-server (remote http(s)/s3 stores) enforces a hard per-actor
# admission cap — both an in-flight *count* and a concurrent *byte* budget —
# and rejects excess concurrent writes outright (HTTP 429) rather than queuing
# them. Independent of _MAX_ATTEMPTS and of surface_conflict (it isn't a
# compare-and-swap race, just admission control). Lives in the shared base
# because witan-code also writes to the deployed omnigraph-server.
#
# THE SERVER'S OWN `Retry-After` IS NOW READ, over the HTTP transport only. The
# CLI's error path prints the JSON body's message and discards the response
# headers, so the subprocess path still has nothing but this blind schedule.
# Where the header IS present it is preferred over the schedule — and where it
# asks for longer than the call has left (REMOTE_CALL_BUDGET_ENV_VAR, when the
# deployment has told us it has a deadline at all) the call is failed
# immediately rather than slept through, since a sleep past the caller's
# cut-off can only end as a torn-down connection. Quoting the server's number
# in the error is worth more than obeying it into a wall.
_ADMISSION_CAP_MARKERS = ("in-flight count cap", "byte budget exceeded")
_ADMISSION_CAP_MAX_ATTEMPTS = 6
_ADMISSION_CAP_BASE_DELAY = 0.25
_ADMISSION_CAP_MAX_DELAY = 4.0

# Budget for the branch-wide recovery barrier (see _RECOVERY_REQUIRED). Modelled
# on the admission cap because it is the same species — the store telling us to
# come back — and deliberately SHORTER, because the measured barrier is brief:
# in the 2026-08-13 repro a plain write issued immediately after the losing race
# succeeded, so the condition clears in well under a second rather than needing
# a restart-length window like _UNAVAILABLE_MAX_WAIT.
#
# Sized so the whole sequence (0.2 + 0.4 + 0.8 + 1.6 + 3.2, capped) stays around
# 6s: long enough to ride out a barrier raised by a concurrent writer, short
# enough that a genuinely stuck branch is reported rather than slept through
# while the caller's deadline burns.
_RECOVERY_MAX_ATTEMPTS = 6
_RECOVERY_BASE_DELAY = 0.2
_RECOVERY_MAX_DELAY = 3.2

# How long one remote call has, end to end, before something ABOVE this library
# stops waiting for it. witan-core does not know that number and must not
# assume one: the same client runs under a hosting layer with a request
# deadline, from an interactive CLI with none, and from a batch Job that may
# happily wait minutes. Whoever deploys it behind a deadline is the only party
# that knows what it is, so they set this; unset means "no known cut-off", and
# a retry hint is then obeyed rather than second-guessed.
#
# What it BUYS when set: sleeping past the caller's deadline cannot succeed. The
# response arrives after the connection is gone, so the write's outcome becomes
# unknowable rather than merely late — the indeterminate-502 failure this whole
# change exists to stop producing. Refusing while there is still time to say so
# turns that into an ordinary, retryable answer.
#
# NOT the same quantity as _ADMISSION_CAP_MAX_DELAY, and keeping them separate
# is the point: that one bounds how long ONE backoff sleep may be, this one
# bounds the WHOLE call including the write-gate queue and every retry so far.
# Using the per-sleep cap as a budget test rejected hints that fit easily and
# admitted sequences of hints that did not.
REMOTE_CALL_BUDGET_ENV_VAR = "WITAN_REMOTE_CALL_BUDGET_SECONDS"
_REMOTE_CALL_BUDGET = 0.0  # 0 = no deadline known; obey the server's hints

# ── Client-side write admission (remote stores only) ─────────────────────────
#
# WHY THIS EXISTS AT ALL: the data tier's admission cap is PER ACTOR, and a
# per-actor cap cannot bound a shared service. Ten users at one write each is
# ten writes in flight with every per-actor cap satisfied, and the deadline that
# actually kills them sees the total, not the per-actor share. Every user's
# write reaches the data tier through ONE process — the single-replica witan MCP
# pod — so this is where a global bound can exist at all.
#
# MEASURED, against the CI data tier directly (2026-08-12, port-forward to
# svc/omnigraph-server, no vMCP and no APISIX, N real threads released from a
# barrier). Single-row inserts into a 1,045-row graph:
#
#     n=1   wall  3.45s     n=4   wall 15.54s     n=8   wall 51.08s (p50 33.29s)
#     n=2   wall  6.49s     n=6   wall 31.20s
#
# Writes are strictly serialised and get *worse* per write as writers are added
# (throughput falls 0.31 → 0.16 req/s), because one single-row insert is a full
# Lance commit cycle against S3 plus a FilteredRead of the whole table. Reads are
# unaffected and hold flat at ~5 req/s, which is why only writes are gated.
#
# So 4 is not a guess — but note what it is calibrated AGAINST: the deployment
# these numbers were measured on stops waiting for a call after 30s. At n=4
# every admitted write still lands inside that; at n=6 the slowest is already
# past it. Admitting a fifth writer does not make it faster — it makes it a 502
# whose outcome the caller cannot even determine
# (tk-a-502-from-the-deployed-witan-does-not-mean-the--f76dcb). A refusal here
# is strictly better: it is unambiguous, and it happens BEFORE anything was
# sent.
#
# A deployment with a different deadline — or none — has a different right
# answer, which is why this is a default and not a constant. witan-core itself
# holds no opinion about who is waiting on the other end.
#
# PER GRAPH, not per server, because the serialisation is: 4 writes to
# `code-bridge` and 4 to `code-github-com-mitodl-lehrer` fired together each
# finished in their solo time. A repo's reindex must not block a memory write.
#
# The wait is bounded by what the measured deployment's deadline could still
# afford: a full gate of 4 drains in ~15.5s, so a write that queues longer than
# ~10s could not finish inside its 30s even once admitted. Waiting past that
# only converts a clean refusal into a torn-down connection.
#
# ★ BOTH ARE DEFAULTS, NOT FIXED VALUES. Each is overridable by an env var, so
# retuning a deployment is a Deployment env change rather than a code change +
# release + image rebuild — the same reasoning as HTTP_TRANSPORT_ENV_VAR above,
# and it matters more here: these numbers were derived from ONE measurement of
# ONE data tier, and the next thing that changes them is the write-cost work
# upstream (tk-upstream-omnigraph-a-single-row-insert-costs-a-f-eeeae3), which
# will move the knee without touching this repo. A value that has to be right
# in a release is a value nobody can correct during an incident.
#
# Read AT CALL TIME (see `_WriteGate.admit`), which is what makes both the env
# override and a test's monkeypatch take effect on the very next call —
# [[les-witan-core-monkeypatch-constants]].
REMOTE_WRITE_MAX_INFLIGHT_ENV_VAR = "WITAN_REMOTE_WRITE_MAX_INFLIGHT"
REMOTE_WRITE_QUEUE_WAIT_ENV_VAR = "WITAN_REMOTE_WRITE_QUEUE_SECONDS"
_REMOTE_WRITE_MAX_INFLIGHT = 4
_REMOTE_WRITE_QUEUE_WAIT = 10.0

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
    #: The server's own `Retry-After`, in seconds, when it sent one. HTTP
    #: transport only — the CLI never sees response headers — so `None` here
    #: means "no hint", not "retry now".
    retry_after: float | None = None


def _classify_cli_error(stderr: str) -> str:
    """Map an omnigraph CLI failure's stderr onto the shared kinds."""
    lowered = stderr.lower()
    if any(m in lowered for m in _UNAVAILABLE_MARKERS):
        return _http.UNAVAILABLE
    if any(m in lowered for m in _ADMISSION_CAP_MARKERS):
        return _http.ADMISSION_CAP
    # Ahead of _RETRYABLE deliberately — see _PRECONDITION_FAILED. Terminal
    # beats retryable when a message could be read as either.
    if any(m in lowered for m in _PRECONDITION_FAILED):
        return _http.PRECONDITION_FAILED
    if any(m in lowered for m in _RECOVERY_REQUIRED):
        return _http.RECOVERY_REQUIRED
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


def shared_transport(server_url: str) -> _http.PooledTransport:
    with _TRANSPORTS_LOCK:
        transport = _TRANSPORTS.get(server_url)
        if transport is None:
            transport = _http.PooledTransport(server_url)
            _TRANSPORTS[server_url] = transport
        return transport


def _env_override(name: str, default: T, cast: Callable[[str], T]) -> T:
    """``name`` from the environment as a number, or ``default``.

    A BAD VALUE FALLS BACK RATHER THAN RAISING, and says so in the log. These
    are read on the write path of a deployed server: a typo in a Deployment env
    var must not turn every write into a crash, which is the one failure mode
    worse than the mis-sizing it was trying to correct. The log line is what
    keeps that from being silent — a knob that appears to be set and is not is
    how a tuning session ends up chasing the wrong variable for an hour.

    Zero is a legal value (it disables admission entirely, which is a real
    operational answer during an incident). Negative is not — it would mean an
    unbounded wait or a gate nobody can pass — so it is refused like a typo.

    NON-FINITE IS REFUSED TOO, and it is the one that does not look like a typo.
    ``float("nan")`` and ``float("inf")`` are both accepted by ``float()`` and
    both survive a ``< 0`` test — ``nan`` compares False against everything —
    so a ``WITAN_REMOTE_WRITE_QUEUE_SECONDS=nan`` would reach
    ``Condition.wait(nan)`` and raise out of the gate. That is precisely the
    "a bad env var turns every write into a crash" failure this function exists
    to prevent, arriving through the one input that passes every other check.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        _log.warning("%s=%r is not a number; using %r", name, raw, default)
        return default
    if not math.isfinite(value):
        _log.warning("%s=%r is not finite; using %r", name, raw, default)
        return default
    if value < 0:
        _log.warning("%s=%r is negative; using %r", name, raw, default)
        return default
    return value


class _WriteGate:
    """Bounded, per-graph admission for remote writes made by this process.

    PROCESS-wide for the same reason the transports above are, and for a
    stronger one: a bound that lived on the client would bound nothing, since
    witan builds a fresh ``OmnigraphClient`` per request (ADR-0004). Shared
    state is the whole mechanism here rather than an optimisation — this IS the
    global limiter the per-actor cap in the data tier cannot be. See the sizing
    note on ``_REMOTE_WRITE_MAX_INFLIGHT``.

    One condition variable guards a count per graph key. ``notify_all`` rather
    than ``notify`` because waiters for *different* graphs share it: waking one
    arbitrary waiter can wake somebody whose graph is still full while the
    thread that could have proceeded goes on sleeping.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._in_flight: dict[str, int] = {}
        #: EWMA of how long an ADMITTED write takes to execute, per graph.
        #: See ``_record`` for why this is execution time and not the wait.
        self._service: dict[str, float] = {}

    @contextmanager
    def admit(
        self, key: str, label: str, call_deadline: float | None = None
    ) -> Iterator[None]:
        """Hold one write slot for ``key``, or raise :class:`WriteQueueFull`.

        Both bounds are resolved HERE, on entry, rather than captured at import
        — a generator-based context manager runs its body at ``__enter__``, so
        an env override set on the Deployment and a test's monkeypatch both take
        effect on the very next call
        ([[les-witan-core-monkeypatch-constants]]).

        ★ ``call_deadline`` IS WHAT MAKES THIS AN ADMISSION POLICY RATHER THAN A
        QUEUE. Bounding concurrency and capping the wait does not ask the only
        question that matters — *will this finish in time?* — so the gate used
        to admit a write that waited 3s for a slot and then ran for 40s. Watched
        live at 24 writers: 56 handlers, durations climbing 3s → 73s, 26 of them
        past the caller's 30s deadline, and ZERO refusals, because a slot always
        freed inside the 10s cap. It let through exactly the writes that strand.

        Now a write is admitted only if the graph's measured service time still
        fits in what the call has left. Time spent queueing is charged
        automatically: ``call_deadline`` is absolute, so every second waited
        shrinks the remaining budget the check is made against.
        """
        limit = _env_override(
            REMOTE_WRITE_MAX_INFLIGHT_ENV_VAR, _REMOTE_WRITE_MAX_INFLIGHT, int
        )
        wait = _env_override(
            REMOTE_WRITE_QUEUE_WAIT_ENV_VAR, _REMOTE_WRITE_QUEUE_WAIT, float
        )
        wait_deadline = time.monotonic() + wait
        with self._cv:
            while True:
                # ★ CHECKED BEFORE EVERY WAIT, not once after the queue clears.
                # Asking only on the way out let a write sit the full queue
                # timeout and THEN be refused for a reason that was already
                # true when it arrived — burning the caller's budget to reach a
                # verdict this could have given immediately. The whole point is
                # to stop consuming a deadline that cannot be met.
                self._refuse_if_it_cannot_finish(key, label, call_deadline)
                if self._in_flight.get(key, 0) < limit:
                    break
                waitable = wait_deadline - time.monotonic()
                # And the wait is bounded by the last moment admission could
                # still be viable: `wait()` does not wake when the budget runs
                # out, so without this the thread sleeps past its own deadline
                # and is refused late for having waited.
                viable = self._latest_viable_wait(key, call_deadline)
                if viable is not None:
                    waitable = min(waitable, viable)
                if waitable <= 0:
                    raise WriteQueueFull(
                        f"omnigraph {label} was refused before it was sent: "
                        f"{self._in_flight.get(key, 0)} writes are already in "
                        f"flight against {key}, and waiting longer cannot help "
                        "— the slot would not free in time for this call to "
                        "finish. NOTHING WAS WRITTEN — retry once the burst "
                        "clears."
                    )
                self._cv.wait(waitable)
            self._in_flight[key] = self._in_flight.get(key, 0) + 1
        started = time.monotonic()
        try:
            yield
        finally:
            with self._cv:
                self._record(key, time.monotonic() - started)
                remaining_writes = self._in_flight[key] - 1
                if remaining_writes:
                    self._in_flight[key] = remaining_writes
                else:
                    del self._in_flight[key]
                self._cv.notify_all()

    def _latest_viable_wait(
        self, key: str, call_deadline: float | None
    ) -> float | None:
        """Seconds this write may still wait and hope to finish, or ``None``.

        Caller holds ``self._cv``. ``None`` means there is nothing to bound the
        wait by — no declared deadline, or no measured service time yet — and
        the queue timeout is the only limit that applies.
        """
        if call_deadline is None:
            return None
        estimate = self._service.get(key)
        if estimate is None:
            return None
        return (call_deadline - estimate) - time.monotonic()

    def _refuse_if_it_cannot_finish(
        self, key: str, label: str, call_deadline: float | None
    ) -> None:
        """Refuse a write the graph cannot serve inside the caller's budget.

        Caller holds ``self._cv``.

        ★ NEVER REFUSES AN IDLE GRAPH. If nothing is in flight, this write is
        the only thing the graph has to do, and refusing it would be a deadlock
        dressed as backpressure: a graph whose service time exceeds the budget
        would decline every write forever and never gather a faster sample to
        recover on. Under load the same rule keeps the gate honest — the
        estimate can only refuse work that is genuinely queued behind something.

        With no samples yet the gate falls back to the pre-existing behaviour.
        A cold process must not refuse the very writes it needs in order to
        learn what a write costs.
        """
        if call_deadline is None or not self._in_flight.get(key):
            return
        estimate = self._service.get(key)
        if estimate is None:
            return
        remaining = call_deadline - time.monotonic()
        if estimate <= remaining:
            return
        raise WriteQueueFull(
            f"omnigraph {label} was refused before it was sent: writes to "
            f"{key} are currently taking about {estimate:.0f}s and this call "
            f"has {max(remaining, 0.0):.0f}s left, so it cannot complete in "
            "time. NOTHING WAS WRITTEN — retry once the burst clears. "
            "(Admitting it anyway is how a write ends up committed while the "
            "caller is told it failed.)"
        )

    #: How fast the service-time estimate reacts. Deliberately asymmetric —
    #: quick to believe things got slower, slow to believe they got faster.
    #: The failure being prevented is admitting during a slowdown, and a mean
    #: that decays as fast as it climbs spends most of a burst under-predicting
    #: exactly when the tail is what stranded the write.
    _RISE = 0.5
    _FALL = 0.1

    def _record(self, key: str, elapsed: float) -> None:
        """Fold one write's EXECUTION time into the estimate for ``key``.

        Caller holds ``self._cv``.

        ★ EXECUTION, NOT TOTAL. ``elapsed`` is measured from admission, after
        any queueing — feeding the wait back in would double-count it: the
        queue would inflate the estimate, the inflated estimate would refuse
        more, and one burst would leave the gate convinced every write costs a
        minute. The measured 3s → 73s spread is mostly queue, not service.
        """
        previous = self._service.get(key)
        if previous is None:
            self._service[key] = elapsed
            return
        alpha = self._RISE if elapsed > previous else self._FALL
        self._service[key] = previous + alpha * (elapsed - previous)


_WRITE_GATE = _WriteGate()


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


def store_cli_args(graph_uri: str, graph_id: str | None = None) -> list[str]:
    """The CLI flags addressing ``graph_uri``, for a store with no client.

    The free-function form of :meth:`OmnigraphClient._store_args`, which
    delegates here. It exists because a tool can legitimately need to address a
    store it holds no client for — ``witan migrate merge`` drives *two* stores
    (a source and a target) through one client's binary, and hardcoding
    ``--store`` there is what made a deployed graph unreachable from the merge
    path: omnigraph 0.8.1 rejects an http(s) ``--store``, so a remote target
    failed at the first ``export``.

    A remote graph id may come from ``graph_id`` or be encoded in the URI as
    ``.../graphs/<id>`` — the latter is what lets a caller name a remote store
    completely in one argument, with no second flag to thread through.
    """
    if graph_uri.startswith(_SERVER_SCHEMES):
        server_url, resolved = _split_server_uri(graph_uri, graph_id)
        return ["--server", server_url, "--graph", resolved]
    return ["--store", graph_uri]


def store_subprocess_env(graph_uri: str, token: str | None = None) -> dict:
    """The subprocess environment for an omnigraph CLI call against ``graph_uri``.

    The free-function form of :meth:`OmnigraphClient._subprocess_env`, with the
    same two rules: a local path or ``s3://`` root has no server to authenticate
    to, so an ambient bearer token is *stripped* rather than merely unset (it
    would otherwise leak into a subprocess with no use for it); a remote store
    takes ``token`` when given and otherwise inherits whatever the environment
    already carries, which is the CLI's own documented fallback.
    """
    env = dict(os.environ)
    if not graph_uri.startswith(_SERVER_SCHEMES):
        env.pop(BEARER_TOKEN_ENV_VAR, None)
    elif token:
        env[BEARER_TOKEN_ENV_VAR] = token
    return env


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


def _recovery_backoff(attempt: int) -> float:
    return _jittered_backoff(attempt, _RECOVERY_BASE_DELAY, _RECOVERY_MAX_DELAY)


# ── Re-entrant per-store write lock ───────────────────────────────
#
# Stores flock cannot coordinate: an http(s) server (writers are other
# processes on other hosts) and an s3:// root (no local file to lock). A
# superset of _SERVER_SCHEMES on purpose — s3 is not a *remote server*, but it
# is just as unlockable.
_UNLOCKABLE_SCHEMES = ("http://", "https://", "s3://")
#
# flock is held by the OPEN FILE DESCRIPTION, not by the process, so a second
# `open()` of the same `<store>.lock` blocks even from the thread that already
# holds it. That makes the obvious nesting — hold the lock across a merge, and
# have the load inside it take the lock too — a self-deadlock rather than the
# no-op you would expect from a mutex. So re-entrancy is tracked here instead.
#
# Keyed by (thread, lock path), NOT by process: two threads doing unrelated
# writes to one store still have to exclude each other, and a process-wide
# counter would hand the second one a lock the first is holding.
_held_flocks: dict[tuple[int, str], int] = {}
_held_flocks_guard = threading.Lock()


def acquire_store_flock(store: str):
    """Take ``<store>.lock`` exclusively, or note a re-entry if already held.

    Returns the open handle to release, or ``None`` when this thread already
    holds the lock — in which case the caller must still call
    :func:`release_store_flock`, which decrements the depth rather than
    unlocking. The ``None`` return is deliberately the same "nothing to
    release" signal the remote/no-lock case already uses.
    """
    key = (threading.get_ident(), str(Path(f"{store}.lock")))
    with _held_flocks_guard:
        if _held_flocks.get(key):
            _held_flocks[key] += 1
            return None
    lock_path = Path(key[1])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")  # noqa: SIM115 — released by release_store_flock
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except BaseException:
        fh.close()
        raise
    with _held_flocks_guard:
        _held_flocks[key] = 1
    return fh


def release_store_flock(store: str, fh) -> None:
    """Undo one :func:`acquire_store_flock`, unlocking at depth zero."""
    key = (threading.get_ident(), str(Path(f"{store}.lock")))
    with _held_flocks_guard:
        depth = _held_flocks.get(key, 0)
        if depth > 1:
            _held_flocks[key] = depth - 1
            return
        _held_flocks.pop(key, None)
    if fh is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


class StoreUnavailable(RuntimeError):
    """The store could not be reached for the whole ``_UNAVAILABLE_MAX_WAIT`` budget.

    A ``RuntimeError`` subclass, so every existing caller keeps catching it
    unchanged; it exists so a caller that can say something *useful* about an
    unreachable store does not have to string-match the message to recognise
    one. The distinction that matters to a user is that this failure is
    transient and the operation is safe to retry — which is exactly what the
    generic "omnigraph … failed" cannot claim.

    Raised only after the budget is exhausted. A connect failure that recovers
    within it never surfaces at all, which is the point of the budget.
    """


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


class WriteIndeterminate(RuntimeError):
    """A write whose outcome CANNOT BE DETERMINED from the response.

    Raised for `recovery_required` on a write. The store is telling us a
    recovery operation is outstanding on the branch, and upstream #470 is
    explicit that the request's "table effects may already require recovery" —
    so this request may have landed, wholly or partly, and the response does
    not say which.

    ★ WHY THIS IS TERMINAL RATHER THAN RETRIED, WHICH IS A REVERSAL. An earlier
    version of this code retried it on a short budget, reasoning from a 0.9.0
    repro where the same message was a purely transient BYSTANDER BARRIER: six
    concurrent appends to distinct keys, five losers, and a write issued
    immediately afterwards succeeded with nothing to repair. That reading is not
    wrong — it is just not the ONLY thing this response means, and the two are
    indistinguishable on the wire.

    Given one signal covering both an effect-free barrier and a possibly-applied
    write, a shorter retry window does not make the retry safe: it only narrows
    the window in which a duplicate is created after recovery rolls the original
    forward. So a write stops here and says so, and the caller re-reads.

    A READ carrying the same condition is still retried — repeating a query
    cannot duplicate anything, and the barrier does clear on its own.

    ★ AND IT IS NOT A `surface_conflict` OUTCOME. A CAS caller must lose a
    GENUINE race, and this is not evidence of one: the barrier fires for writers
    contending with nobody. Reporting it as `lost_race` would be a confident
    wrong answer, which is worse than an error the caller can see.

    Sibling of the transport-level indeterminacy the proxy already raises for a
    502 — same species of unknowable outcome, same advice: re-read before
    retrying, because retrying blind writes it twice if it did land.
    """


class WriteQueueFull(RuntimeError):
    """This process already has as many writes in flight against one graph as the
    graph can serve inside the caller's deadline, and the wait for a slot expired.

    ★ THE ONE THING THIS SAYS THAT A 502 CANNOT: NOTHING WAS WRITTEN. ★
    The failure this replaces is a write admitted into a queue it could not clear
    in time, torn down mid-flight, and surfaced as a gateway error whose outcome
    nobody can determine — the write usually committed, sometimes did not, and
    the response distinguishes neither. Refusing before anything is sent turns
    that into an ordinary, retryable answer.

    A ``RuntimeError`` subclass so every existing ``except RuntimeError`` renders
    it as one red line, and its own type so a caller that wants to queue and
    retry can recognise it without matching on prose.
    """


def _is_storage_version_mismatch(msg: str) -> bool:
    lowered = msg.lower()
    return all(marker in lowered for marker in _STORAGE_VERSION_MISMATCH_MARKERS)


def _friendly_storage_error(raw: str, hint: str) -> str:
    """Strip the Rust panic's ANSI codes and backtrace boilerplate ("Location:",
    "Backtrace omitted...") from a storage-version-mismatch error, keeping just
    omnigraph's own message, and append the caller's remediation ``hint``."""
    cleaned = _ANSI_RE.sub("", raw).split("Location:")[0].strip()
    return f"{cleaned}\n\n{hint}"


def _commit_id_from_mutate_body(body: str) -> str | None:
    """Pull ``commit.graph_commit_id`` out of a raw mutate response body.

    Deliberately lenient rather than raising: an empty body, non-JSON body, or
    a body with no ``commit`` (a pre-#470 server) are all just "no commit id
    available", which callers already treat as a normal degrade — the same
    posture as :meth:`OmnigraphClient.read_with_commit` returning ``None``.
    A write itself must never fail because this one piece of BONUS
    information could not be parsed out of an otherwise-successful response.
    """
    if not body.strip():
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    commit = parsed.get("commit")
    if not isinstance(commit, dict):
        return None
    graph_commit_id = commit.get("graph_commit_id")
    return graph_commit_id if isinstance(graph_commit_id, str) else None


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

    def read_with_commit(
        self,
        query_file: str,
        query_name: str,
        params: dict,
    ) -> tuple[list[dict], str | None]:
        """:meth:`read`, plus the ``graph_commit_id`` its rows were read at.

        That token is the input to :meth:`change`'s ``if_commit``: it names the
        branch head this snapshot came from, so a later write can demand the
        head has not moved since. Read and write have to come from the SAME
        snapshot for the pair to mean anything, which is why this returns both
        together rather than offering a separate "what is the head now" call —
        a head fetched independently is already stale by construction.

        ``None`` when the server did not supply one: a pre-#470 omnigraph, or
        the CLI path, which prints rows and discards the envelope. A caller must
        treat ``None`` as "no precondition available" and fall back to the
        best-effort path rather than passing it on — ``if_commit=None`` means an
        UNCONDITIONAL write, which is exactly what you did not ask for.
        """
        rows, envelope = self._read_rows(query_file, query_name, params)
        commit = envelope.get("graph_commit_id") if isinstance(envelope, dict) else None
        return rows, commit

    def read(
        self,
        query_file: str,
        query_name: str,
        params: dict,
    ) -> list[dict]:
        """Run a named read query. Returns a list of result rows."""
        return self._read_rows(query_file, query_name, params)[0]

    def _read_rows(
        self,
        query_file: str,
        query_name: str,
        params: dict,
    ) -> tuple[list[dict], dict | None]:
        """The shared read path: rows, plus the response envelope they came in.

        One implementation behind both :meth:`read` and
        :meth:`read_with_commit` on purpose — the alias-stripping and the
        0.7.0-envelope handling below are exactly the kind of parsing that
        drifts when it is copied, and a drift here would show up as a read that
        works on one path and silently returns different keys on the other.
        """
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
            return [], None
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"omnigraph returned non-JSON: {result!r}") from exc
        # v0.7.0 wraps results in {rows: [...], columns: [...], ...}
        envelope = parsed if isinstance(parsed, dict) else None
        rows = parsed.get("rows", parsed) if isinstance(parsed, dict) else parsed
        # strip alias prefixes: "p.slug" → "slug"
        stripped = [{k.split(".", 1)[-1]: v for k, v in row.items()} for row in rows]
        return stripped, envelope

    def change(
        self,
        query_file: str,
        query_name: str,
        params: dict,
        *,
        surface_conflict: bool = False,
        if_commit: str | None = None,
    ) -> str | None:
        """Run a named mutation query.

        The optional ``guard`` runs first: it may raise to reject the write, or
        return rewritten params (e.g. with secrets redacted) that are what
        actually get persisted.

        ``surface_conflict``: raise :class:`OmnigraphConflict` on an
        optimistic-concurrency conflict instead of transparently retrying the
        write. Callers implementing a compare-and-swap (e.g. ``task_claim``) need
        this so a lost race is detectable rather than silently clobbered.

        ``if_commit``: a ``graph_commit_id`` from :meth:`read_with_commit`. The
        write applies ONLY while that branch head is still current; otherwise it
        is refused, terminally, having changed nothing.

        ★ THE TWO ARE COMPLEMENTARY, NOT ALTERNATIVES, and a CAS caller wants
        both. ``if_commit`` is what makes losing a race a FACT rather than an
        inference — without it the claim path is read, write, re-read and hope.
        ``surface_conflict`` is what turns that fact into an
        :class:`OmnigraphConflict` the caller can catch instead of a hard error.

        ★ AND ``if_commit`` IS COARSE. The precondition is the whole branch
        head, not this row: ANY concurrent write to the graph invalidates the
        token, so a rival claim and an unrelated ``memory_store`` are
        indistinguishable. That makes false conflicts a function of total write
        traffic rather than of contention on what you are claiming. It is still
        worth having — a refusal is now truthful — but a caller must expect to
        re-read and retry more often than the contention alone would suggest.

        Returns the NEW ``graph_commit_id`` this write produced (omnigraph
        #470's ``ChangeOutput.commit.graph_commit_id``), or ``None`` when the
        tier does not supply one. Distinct from ``if_commit``, which is the
        commit you fenced ON before the call — this is the commit that exists
        AFTER it, and is a floor a caller can compare a later unconstrained
        read's own reported commit against, to tell a genuinely fresher read
        apart from one that is still stale (omnigraph's commit ids are ULIDs —
        docs/user/concepts/storage.md — so string comparison orders them).
        Empirically confirmed via HTTP (2026-08-18): the server always answers
        a mutate with the new commit inline, so no separate read is needed to
        learn it.

        ★ HTTP ONLY. The CLI path always returns ``None`` here, DELIBERATELY —
        see the paragraph in the CLI branch below before trying to close that
        gap with ``--json``.
        """
        if self.guard is not None:
            params = self.guard(query_name, params)
        transport = self._http_transport()
        if transport is not None:
            body = self._http_execute(
                transport,
                "mutate",
                self._query_source(query_file, query_name),
                params,
                "mutate",
                surface_conflict=surface_conflict,
                if_graph_commit=if_commit,
            )
            return _commit_id_from_mutate_body(body)
        # ★ DO NOT ADD --json HERE TO CLOSE THIS GAP. Verified empirically
        # against the real CLI, 2026-08-18: a LOST --if-commit race reports its
        # failure differently depending on --json. Without it, the message
        # lands on STDERR — "precondition failed on branch 'main': expected
        # head '…' but current is …" — which is exactly what
        # `_classify_cli_error`'s `_PRECONDITION_FAILED` markers are tuned
        # against. WITH --json, that same failure moves ENTIRELY to a JSON
        # body on STDOUT and STDERR COMES BACK EMPTY. `_execute`'s `attempt()`
        # classifies from `result.stderr` only, so adding --json here would
        # silently starve that classifier on every CLI-path precondition
        # failure — the exact 412 handling shipped in agent-kit#245/#246 this
        # morning. The CLI path stays without a returned commit id rather than
        # risk that; a caller degrades to an unconstrained verification read,
        # same as it already does when `if_commit` itself is unavailable.
        extra = ["--if-commit", if_commit] if if_commit is not None else []
        self._run(
            "mutate",
            "--query",
            str(self.queries_dir / query_file),
            query_name,
            "--params",
            json.dumps(params),
            *extra,
            surface_conflict=surface_conflict,
        )
        return None

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
        return shared_transport(self.server_url)

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
        if_graph_commit: str | None = None,
    ) -> str:
        """Run one query/mutate over HTTP under the shared retry policy.

        A write additionally holds a slot in the process-wide :data:`_WRITE_GATE`
        for the whole call, retries included — a write being retried is still
        occupying the graph, and accounting for it any other way would let the
        backoff sleeps hide a queue that is still there. Reads are ungated: they
        do not serialise (~5 req/s flat from 8 to 36 concurrent readers), so a
        bound on them would only add latency to the one path that has none.
        """
        is_write = verb == "mutate"

        token = self._resolve_token()

        def attempt() -> _AttemptResult:
            if not is_write:
                outcome = transport.query(self.graph_id, source, params, token)
            elif if_graph_commit is None:
                # Deliberately not `mutate(..., None)`: an unconditional write
                # calls the transport exactly as it always did. Keeping the
                # argument off the call entirely is what lets the precondition
                # be additive — nothing that does not ask for one can be
                # affected by its existence, including the several test doubles
                # implementing the older signature.
                outcome = transport.mutate(self.graph_id, source, params, token)
            else:
                outcome = transport.mutate(
                    self.graph_id, source, params, token, if_graph_commit
                )
            return _AttemptResult(
                outcome.kind,
                body=outcome.body,
                error=outcome.error,
                retry_after=outcome.retry_after,
            )

        return self._with_retry_policy(
            attempt,
            label,
            # Only consulted if a repair is needed, which still shells out —
            # `repair` has no HTTP form, so the CLI env is what it runs with.
            env=self._subprocess_env(),
            is_write=is_write,
            surface_conflict=surface_conflict,
        )

    def export_to(self, path: Path | str, *, label: str = "export") -> None:
        """Stream ``omnigraph export`` of this store into ``path``.

        The streaming counterpart to :meth:`_run`. Everything else runs through
        ``_execute``, which captures stdout into a string — fine for a query
        result, wrong for an export, which is the *entire* graph and would sit
        fully buffered in memory (and then again as the parsed rows). So this
        redirects the subprocess straight to a file.

        What it does **not** fork is the retry policy: the attempt is classified
        by the same :func:`_classify_cli_error` and driven by the same
        :meth:`_with_retry_policy` as every other call, so an export rides out a
        server restart instead of dying on the connect refusal. That is the
        whole point of it living here rather than in a caller's own
        ``subprocess.run`` — see ``witan.server.store_merge``, which used to
        reach for ``_binary``/``_store_args`` and thereby opt out.

        ``label`` names the export in any error message ("export (target)"),
        since a merge runs two against different stores.
        """
        cmd = [self._binary, "export", *self._store_args()]
        env = self._subprocess_env()
        out = Path(path)

        def attempt() -> _AttemptResult:
            # Truncate per attempt ("w", not "a"): a retry must overwrite
            # whatever the failed attempt already streamed, or a server that
            # dies mid-export leaves a prefix that the successful retry then
            # appends a second full copy onto.
            try:
                with open(out, "w", encoding="utf-8") as fh:
                    result = subprocess.run(
                        cmd, stdout=fh, stderr=subprocess.PIPE, text=True, env=env
                    )
            except OSError as exc:
                raise RuntimeError(f"omnigraph {label} could not run: {exc}") from exc
            if result.returncode == 0:
                return _AttemptResult(_http.OK, returncode=0)
            return _AttemptResult(
                _classify_cli_error(result.stderr),
                error=result.stderr,
                returncode=result.returncode,
            )

        # is_write=False: an export takes no write lock, and its retries are
        # unconditionally safe because repeating a read changes nothing.
        self._with_retry_policy(attempt, label, env=env, is_write=False)

    def load_batch(self, records: list[dict], mode: str = "merge") -> str:
        """Bulk-load one batch of node/edge records via ``omnigraph load``.

        Returns the CLI's stdout (empty for an empty batch) — the load summary,
        which ``merge_store`` reports back to the user. Most callers ignore it.

        Each record is a JSONL line: ``{"type": Node, "data": {...}}`` for a
        node or ``{"edge": Edge, "from": key, "to": key}`` for an edge. One
        subprocess replaces a ``mutate`` per record.

        Deliberately **one** batch and no splitting — a caller writing to a
        deployed server must bound the batch itself with
        :func:`witan_core.chunking.chunk_records`, because what a batch may
        contain depends on the caller's record graph (every node before any
        edge) and on ``mode`` (``overwrite`` truncates the node type, so it can
        never be split). Encoding either here would make this primitive lie to
        one of its callers.
        """
        if not records:
            return ""
        fd, tmp = tempfile.mkstemp(suffix=".jsonl", prefix="omnigraph-load-")
        try:
            # Explicit UTF-8, not the platform default: witan rows carry prose
            # (memory content, task descriptions) that is routinely non-ASCII,
            # and on a non-UTF-8 locale the default encoding either mangles it
            # or raises before `omnigraph load` ever sees the file.
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record))
                    fh.write("\n")
            return self._run("load", "--data", tmp, "--mode", mode)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _store_args(self) -> list[str]:
        """The CLI flags that address this store. A remote omnigraph-server
        (http(s)) is ``--server <url> --graph <id>`` (omnigraph 0.8.1 rejects an
        http(s) ``--store``); local paths and s3:// roots are ``--store <uri>``.

        Passes the already-resolved ``graph_id`` rather than re-deriving it, so
        a client built from a bare server URL plus ``WITAN_MEMORY_GRAPH`` keeps
        addressing the same graph.
        """
        return store_cli_args(self.graph_uri, self.graph_id)

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
        # A local path or an s3:// root is opened directly — there is no server
        # to present a bearer token to, and s3 authenticates with AWS
        # credentials instead, so an ambient token is stripped rather than
        # merely not set. A remote store with no explicit token deliberately
        # keeps whatever the environment already carries: that is the CLI's own
        # documented fallback (see BEARER_TOKEN_ENV_VAR), and `export
        # OMNIGRAPH_BEARER_TOKEN=…` with no token in config is a supported way
        # to drive it. Both rules live in `store_subprocess_env`.
        return store_subprocess_env(self.graph_uri, self.token)

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

        ★ AND THAT IS WHY WRITE ADMISSION LIVES HERE, not in ``_http_execute``.
        Being the one place every call converges makes it the only place a bound
        cannot be walked around. Gating the HTTP branch alone left two real
        remote-write paths ungated:

        * ``load_batch`` shells out to ``omnigraph load`` — it has no HTTP form
          at all — so every batch of a ``store_merge`` bypassed the bound. Two
          people migrating at once is precisely the multi-user burst this exists
          to bound.
        * witan-code's BRANCHED clients deliberately fall back to the CLI
          (``_transport`` returns ``None`` when ``_extra_args`` is non-empty,
          because 0.8.1's HTTP API has no request-side branch selector), so
          every write from a branch view bypassed it too.

        The deadline is established here for the same reason, and BEFORE the gate
        is entered: the queue wait is the largest single consumer of the budget,
        and one measured from after admission would let a write sit 10s in the
        gate and still believe it had its whole budget left to retry.
        """
        # Remote writes only. A local store is serialised by its own flock and
        # has no server to saturate, and reads do not queue (~5 req/s flat from
        # 8 to 36 concurrent readers) so bounding them would only add latency to
        # the one path that has none.
        gate_write = is_write and self.is_remote
        budget = (
            _env_override(REMOTE_CALL_BUDGET_ENV_VAR, _REMOTE_CALL_BUDGET, float)
            if self.is_remote
            else 0.0
        )
        deadline = time.monotonic() + budget if budget else None
        gate = (
            _WRITE_GATE.admit(
                f"{self.server_url}|{self.graph_id}", label, call_deadline=deadline
            )
            if gate_write
            else nullcontext()
        )
        with gate:
            return self._retry_loop(
                attempt_once,
                label,
                env=env,
                is_write=is_write,
                surface_conflict=surface_conflict,
                deadline=deadline,
            )

    def _retry_loop(
        self,
        attempt_once: Callable[[], _AttemptResult],
        label: str,
        *,
        env: dict,
        is_write: bool,
        surface_conflict: bool,
        deadline: float | None,
    ) -> str:
        """The loop itself, inside whatever admission :meth:`_with_retry_policy`
        decided on. ``deadline`` is a ``time.monotonic()`` stamp past which this
        call cannot usefully still be running, or ``None`` when no cut-off is
        known — in which case a ``Retry-After`` is obeyed rather than
        second-guessed."""
        lock_fh = self._acquire_write_lock(is_write)
        try:
            attempt = 0
            admission_cap_attempt = 0
            recovery_attempt = 0
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
                    raise StoreUnavailable(
                        f"omnigraph {label} failed after {unavailable_attempt} "
                        f"attempts over {elapsed:.0f}s — could not connect to "
                        f"{self.server_url}:\n{err.strip()}"
                    )
                if kind == _http.ADMISSION_CAP:
                    # Independent budget/backoff from the drift retries below —
                    # doesn't consume _MAX_ATTEMPTS and ignores surface_conflict.
                    #
                    # The server's own Retry-After wins over the blind schedule
                    # when there is one — but only when THE CALL CAN STILL
                    # AFFORD IT, and that is measured against the remaining
                    # budget, not against _ADMISSION_CAP_MAX_DELAY. Those are
                    # different quantities and conflating them was wrong in both
                    # directions: it rejected a `Retry-After: 5` that fits
                    # comfortably inside a 30s budget, while happily sleeping
                    # five separate 4s hints for a total of 20s on top of
                    # whatever the write gate had already spent queueing —
                    # arriving at exactly the cut-off it meant to stay inside.
                    #
                    # With no deadline (the CLI path) there is no cut-off to
                    # respect and any hint is obeyed, which is the pre-existing
                    # behaviour for that transport.
                    admission_cap_attempt += 1
                    hint = result.retry_after
                    delay = (
                        hint
                        if hint is not None
                        else _admission_cap_backoff(admission_cap_attempt)
                    )
                    remaining = (
                        None if deadline is None else deadline - time.monotonic()
                    )
                    if remaining is not None and delay > remaining:
                        # Sleeping past the deadline can only end as a torn-down
                        # connection whose outcome the caller cannot determine.
                        # Failing NOW, quoting what the server asked for, is the
                        # honest answer — the wait it wants belongs to whoever
                        # schedules the retry, not to a call that cannot extend
                        # its own deadline.
                        asked = (
                            f"which asked to be retried in {hint:.0f}s"
                            if hint is not None
                            else "and the backoff before another attempt"
                        )
                        raise RuntimeError(
                            f"omnigraph {label} was refused by the server's "
                            f"admission cap, {asked} — longer than the "
                            f"{max(remaining, 0.0):.1f}s this call has left. "
                            f"Nothing was written; retry in {delay:.0f}s:\n"
                            f"{err.strip()}"
                        )
                    if admission_cap_attempt < _ADMISSION_CAP_MAX_ATTEMPTS:
                        time.sleep(delay)
                        continue
                    raise RuntimeError(
                        f"omnigraph {label} failed after "
                        f"{_ADMISSION_CAP_MAX_ATTEMPTS} attempts (actor "
                        f"admission cap exceeded):\n{err.strip()}"
                    )
                if kind == _http.RECOVERY_REQUIRED:
                    # ★ A WRITE STOPS HERE. See `WriteIndeterminate`: this one
                    # response covers both an effect-free bystander barrier and
                    # a request whose table effects may already have landed, and
                    # nothing on the wire separates them. Retrying the ambiguous
                    # case duplicates the mutation once recovery rolls the
                    # original forward, and a shorter budget only shrinks the
                    # window in which that happens rather than closing it.
                    #
                    # Deliberately NOT honouring surface_conflict either: the
                    # barrier fires for writers contending with nobody, so
                    # reporting it as a lost race would be a confident wrong
                    # answer.
                    if is_write:
                        raise WriteIndeterminate(
                            f"omnigraph {label} hit a recovery barrier on the "
                            f"branch. ITS OUTCOME IS INDETERMINATE — the write "
                            f"may already have landed, wholly or partly, and "
                            f"the response does not say which. Re-read before "
                            f"retrying; retrying blind writes it twice if it "
                            f"did land:\n{err.strip()}"
                        ) from None
                    # A READ is safe to repeat, and the barrier does clear on
                    # its own — measured self-clearing in under a second. Its
                    # own budget, like the admission cap: neither consumes
                    # _MAX_ATTEMPTS.
                    recovery_attempt += 1
                    if recovery_attempt < _RECOVERY_MAX_ATTEMPTS:
                        time.sleep(_recovery_backoff(recovery_attempt))
                        continue
                    raise RuntimeError(
                        f"omnigraph {label} failed after {_RECOVERY_MAX_ATTEMPTS} "
                        f"attempts — a recovery barrier on the branch kept "
                        f"blocking the read:\n{err.strip()}"
                    )
                if kind == _http.PRECONDITION_FAILED:
                    # TERMINAL. The caller stated a precondition and it was
                    # false, so re-sending this exact write is never right —
                    # upstream never replays it either. A CAS caller still gets
                    # `OmnigraphConflict` so `task_claim` can re-read and answer
                    # `lost_race`; everyone else gets a hard error rather than
                    # a silent retry over the winner.
                    #
                    # Note this raises on the FIRST attempt in both branches: it
                    # does not fall through to the retry counter below, which is
                    # the difference between this and a 409.
                    if surface_conflict:
                        raise OmnigraphConflict(err.strip()) from None
                    raise RuntimeError(
                        f"omnigraph {label} was refused because its stated "
                        f"precondition no longer held — the graph moved since "
                        f"you read it. NOTHING WAS WRITTEN, and this write must "
                        f"not be retried as-is; re-read and decide:\n{err.strip()}"
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
            # Not `if lock_fh is not None`: a re-entrant acquisition returns
            # None *and* has a depth to decrement, so the release has to run
            # either way. It is a no-op when no lock was taken at all (remote
            # store, or a read).
            if is_write and not self.graph_uri.startswith(_UNLOCKABLE_SCHEMES):
                release_store_flock(self.graph_uri, lock_fh)

    def _acquire_write_lock(self, is_write: bool):
        """Hold a per-store exclusive lock for writes (local stores)."""
        if not is_write or self.graph_uri.startswith(_UNLOCKABLE_SCHEMES):
            return None
        return acquire_store_flock(self.graph_uri)

    @contextmanager
    def hold_write_lock(self) -> Iterator[None]:
        """Hold this store's write lock across several operations.

        For a caller whose *sequence* has to be atomic, not just each step —
        ``merge_store`` exports the target, reconciles against it, then loads
        the winners, and another writer landing between the export and the load
        would make the decisions stale.

        The nested writes inside the block re-enter the lock rather than
        blocking on it (see :func:`acquire_store_flock`). A no-op for remote
        stores, matching :meth:`_acquire_write_lock` — flock is a local-file
        mechanism and cannot coordinate writers on a shared server.
        """
        if self.graph_uri.startswith(_UNLOCKABLE_SCHEMES):
            yield
            return
        fh = acquire_store_flock(self.graph_uri)
        try:
            yield
        finally:
            release_store_flock(self.graph_uri, fh)

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
