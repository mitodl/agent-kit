"""Shared base for the omnigraph client.

Both servers drive omnigraph the same way: store addressing in whichever
dialect the installed CLI speaks (``store_cli_args``: ``--store``/``--server``
before 0.14, ``--target`` with a generated operator config from 0.14), a per-store advisory write lock for local stores, a retry loop for
optimistic-concurrency conflicts, self-backoff for the deployed omnigraph-server's
per-actor admission cap and for the window in which it is restarting, and named
read/mutate queries. That LOCAL-vs-REMOTE-generic surface lives here.

TWO TRANSPORTS, ONE POLICY

Calls reach omnigraph either by running the ``omnigraph`` CLI as a subprocess or,
for ``query``/``mutate`` against a deployed server, over a pooled HTTP connection
(:mod:`witan_core.omnigraph_http`) — the subprocess is 77-81% of every remote
read on a compacted store. The HTTP path is NOT a general replacement: omnigraph
0.8.1 serves only ``query``, ``mutate`` and ``graphs`` over HTTP, so ``load``,
``branch``, ``commit``, ``optimize``, ``cleanup`` and ``schema apply``
all still shell out, as does any client that injects extra CLI args
(witan-code's ``--branch`` views). ``_http_transport`` is the single place that
decides.

What must not fork is the POLICY. Both transports produce an ``_AttemptResult``
carrying one of the classification kinds, and ``_with_retry_policy`` runs the
same loop over either — so a restarting server, an admission cap, a
compare-and-swap conflict or a drifted store behaves identically however
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

import atexit
import fcntl
import functools
import hashlib
import ipaddress
import json
import logging
import math
import os
import random
import re
import shutil
import stat
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
from witan_core.group_commit import (
    ConditionFailed,
    GroupCommitter,
    RejectedBatch,
    Step,
)
from witan_core.refusal import Refusal

_log = logging.getLogger(__name__)
T = TypeVar("T", int, float)

# omnigraph local stores use optimistic concurrency (Lance manifest versions) and
# are NOT safe for concurrent writers. We serialize writes with a per-store
# advisory lock (prevention) and, as a safety net, retry transient "stale view"
# conflicts.
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
#
# ★ TWO SPELLINGS OF THE SAME CONDITION, DELIBERATELY. omnigraph's 2026-08-20
# vocabulary refactor (upstream 69d292ce80) reworded these messages —
# "expected manifest table version N" became "expected published dataset
# version N", and "ahead of manifest version N" became "ahead of published
# dataset version N". The old spellings stay because a marker list is matched
# against whatever binary the user has INSTALLED, not against the pinned one:
# `_installed_version` re-downloads only on a version change, and this rename
# shipped without one (both builds report 0.10.0). Dropping the old wording
# would silently stop classifying conflicts on every machine that has not
# re-fetched.
#
# ★ AND THE NEW MARKER CARRIES ITS "expected" PREFIX, which is not decoration.
# The bare phrase "published dataset version" also appears in "historical
# published dataset version N was reclaimed" — a TERMINAL error (the version a
# historical read pinned is gone; retrying re-reads the same absence forever).
# Its old wording was "historical table version …", which contains no
# "manifest table version" and so was correctly fatal; matching the bare phrase
# would newly and wrongly make it retryable. The old marker was this specific
# for the same reason, and the new one has to be too.
_RETRYABLE = (
    "stale view",
    "manifest table version",
    "expected published dataset version",
    "refresh and retry",
    "write authority",
    "reprepare from the current branch",
)
# Checked BEFORE _RETRYABLE in `_classify_cli_error`, which is what keeps the
# drift message — "… ahead of published dataset version 7; run `omnigraph
# repair` …" — classified as drift rather than as a conflict to retry.
#
# ★ DRIFT IS TERMINAL. Neither the 0.13 nor the 0.14 CLI can reconcile it:
# `omnigraph repair` there takes no `--confirm`/`--force` and only diagnoses
# ("Diagnose foreign Lance HEAD movement without adopting it"). The client used
# to shell out to `repair --confirm --force` and retry, which failed argument
# parsing every time and so only burned the attempt budget on the same drift.
_NEEDS_REPAIR = (
    "ahead of manifest",
    "ahead of published dataset version",
    "omnigraph repair",
)

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

# Checked BEFORE _RETRYABLE for the same reason the HTTP side checks it before
# its 409 catch-all: this is terminal, and retrying only delays the one message
# that says what to do. omnigraph 0.10.0 prints, verbatim:
#
#   full-text index '<name>' requires rebuild: <reason>; run omnigraph
#   rebuild-full-text-indexes <URI> --branch <branch> on the live branch
#   (historical snapshots are unchanged)
#
# The command name is the marker rather than "requires rebuild" alone, which is
# generic enough to collide with a future unrelated message.
_FULL_TEXT_REBUILD_REQUIRED = (
    "rebuild-full-text-indexes",
    "full_text_index_rebuild_required",
)
_MAX_ATTEMPTS = 8

# Backoff between optimistic-concurrency retries. FULL jitter, unlike the
# helpers below that add 10%: the losers of an OCC race are contending for the
# same table, and what has to be broken is their lockstep. The old fixed
# `0.05 * attempt` had none at all and spent ~1.4s across all eight attempts,
# which is shorter than one served mutate, so a steady stream of writers to one
# branch view (Sentry WITAN-12) could win every round against a slower one.
_CONFLICT_BASE_DELAY = 0.1
_CONFLICT_MAX_DELAY = 1.0

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
#
# `too_many_requests` is the 429's wire code. A 0.13 read reports its failure
# as the JSON error object, code included, so the code covers whatever the
# server words the cap as (upstream has at least two more spellings than the
# pair here). A write does not print the code and exits 75 instead; see
# `_EXIT_RETRY`.
_ADMISSION_CAP_MARKERS = (
    "in-flight count cap",
    "byte budget exceeded",
    "too_many_requests",
)
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
# That 30s termination half is out of date. It read as "the server ignores
# SIGTERM and is SIGKILLed at the deadline", but a 2026-08-24 CI re-measurement
# already saw the pod go within the second (ol-infrastructure data_tier.py).
# omnigraph 0.11 makes shutdown explicit: the listener closes at SIGTERM, so
# new connections get "tcp connect error" and are retried here, and in-flight
# requests run until OMNIGRAPH_SHUTDOWN_GRACE_SECONDS (30 on the data tier,
# pod grace 35). An idle server exits at once; a request still running at the
# deadline fails mid-flight and is correctly NOT retried. So the drain half is
# now "longest in-flight request, capped at 30s". The boot half is the binary
# opening its S3-backed graphs — the port is still unbound ~19s in. Neither
# half is going to get dramatically faster, so the deadline is set well above
# the observed worst case rather than hugged to it: a slower node, a cold
# image pull, or a larger graph all push the real number up.
#
# 0.13 says something else when the CLI cannot reach the server. It sends
# `HEAD /healthz` before every data request, and when that fails it reports,
# verbatim on 0.13.0 for a refused connection and for an unresolvable host
# alike:
#
#   server discovery connection failed (DNS, TCP, proxy, or TLS); could not
#   establish omnigraph-http-api: 0.13; this data request was not sent
#
# "this data request was not sent" is the property the old markers were
# standing in for, stated outright, so it is safe for a write too. The
# timed-out spelling is from upstream's `graph_http.rs` and was not provoked
# here. Deliberately NOT matched: "could not establish … through successful
# server discovery", which is something that ANSWERED the probe without the
# 0.13 contract. A server on another contract will keep doing so however long
# we wait. A proxy answering 502/503 in a restarting server's place lands
# here too and would clear, but nothing sits between witan and
# omnigraph-server today, and telling the two apart means reading the status
# out of wording that was not provoked here.
#
# The old markers stay: a marker list is matched against whatever binary is
# installed, and that is 0.11 until the pin moves.
_UNAVAILABLE_MARKERS = (
    "tcp connect error",
    "dns error",
    "server discovery connection failed",
    "server discovery timed out",
)

# The 0.13 CLI makes the same check on a data response that
# `_http.contract_violation` makes for the pooled transport, and reports a
# failure of it as (upstream `graph_http.rs`, not provoked here):
#
#   server response did not carry exactly one omnigraph-http-api: 0.13 header;
#   this request's effects are unknown; do not retry automatically
#
# Classified the same way, so a write over the CLI raises WriteIndeterminate
# as one over HTTP does. The discovery refusals say "this data request was not
# sent" instead and do not match.
_CLI_CONTRACT_VIOLATION = "this request's effects are unknown"

# What a 0.13 server answers, with 503 `graph_unavailable`, for a graph it
# knows but is not serving YET (upstream `handlers.rs`). Both clear on their
# own, so they get the same wait as an unreachable server, which is what
# `classify_status` already gives them over HTTP by status. The third 503 with
# that code, a graph that failed to open, does not clear until an operator
# acts; see `_http.is_graph_blocked`.
_GRAPH_NOT_YET_SERVING = (
    "graph is loading",
    "graph admission is closed for a serving transition",
)

# The 0.13 CLI exits with this (EX_TEMPFAIL) for exactly one condition: a
# single effectful request the server refused with 429 `too_many_requests`
# before doing anything (upstream `command_outcome.rs`). That is the admission
# cap, identified without reading prose.
_EXIT_RETRY = 75
_UNAVAILABLE_MAX_WAIT = 150.0
_UNAVAILABLE_BASE_DELAY = 0.5
_UNAVAILABLE_MAX_DELAY = 10.0

# How a bearer token reaches a pre-0.14 omnigraph CLI: its last-resort
# fallback for any server, and the only per-invocation form (no subcommand
# takes a token flag). omnigraph 0.14 no longer reads it at all; there the
# token travels as OPERATOR_TOKEN_ENV_VAR below. witan still reads it as the
# ambient token for a remote store with none configured (`_remote_token`).
#
# Per-invocation matters: witan resolves a DIFFERENT per-actor token per request
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

# omnigraph 0.14 replaced the routing flags (`--store`, `--server`, `--graph`
# on a server URL, `--profile`) with one `--target NAME|ADDRESS`. An inline
# ADDRESS is a local path or `file://` URI only; a server must be a NAMED target
# in an operator config, and its token comes from that target's single `auth`
# provider. witan generates that config itself (`_operator_config`) with
# `auth: {provider: token, source: {env: OPERATOR_TOKEN_ENV_VAR}}` and sets the
# variable on each subprocess, which keeps the per-actor isolation above: the
# file holds no secret and is shared, the token is per call.
#
# The CLI that speaks this is identified by `omnigraph version --json`
# reporting `protocols.target == 2`; a pre-0.14 `version` takes no `--json`
# (0.13 exits 2), which is how the legacy flags stay selected for it.
OPERATOR_TOKEN_ENV_VAR = "WITAN_OMNIGRAPH_TOKEN"
_TARGET_PROTOCOL = 2
_OPERATOR_TARGET = "witan"

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

# Concurrent remote writes from one actor to one graph share a commit (see
# `witan_core.group_commit`). On by default; this is the revert, read per call
# for the same reason as HTTP_TRANSPORT_ENV_VAR.
GROUP_COMMIT_ENV_VAR = "WITAN_GROUP_COMMIT"

# A server answer that proves nothing was applied: the engine validates the
# whole query before any effect, and a resource limit is checked before it too.
# Only these let a failed group be replayed one write at a time; a failure
# whose effects are unknown (a mid-flight timeout is FATAL too) must not be.
# One 400 is NOT proof: omnigraph 0.13 answers a generic DataFusion error with
# 400 and a "query: " message, and marks it completion-uncertain server-side
# (`ApiError::from_omni`, omnigraph-server/src/lib.rs), without saying so on
# the wire. `_DATAFUSION_ERROR_PREFIX` keeps that one out.
_DEFINITE_REJECTION_STATUSES = frozenset({400, 413})
_DATAFUSION_ERROR_PREFIX = "query: "
_PRECONDITION_FAILED_STATUS = 412
_DELETE_STATEMENT_RE = re.compile(r"^\s*delete\b", re.MULTILINE)

# A deployed omnigraph-server is reached over http(s); local files and s3://
# roots are opened directly. Only http(s) needs the server/graph addressing
# split. s3:// is a direct store, which only a pre-0.14 CLI can open.
_SERVER_SCHEMES = ("http://", "https://")
_CLOUD_SCHEMES = ("s3://", "az://")
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


#: The inline GQ statements :meth:`OmnigraphClient.statement` will run, keyed
#: by the verb that carries them. A CLOSED SET, not "whatever the engine would
#: accept" — see that method for why the write path cannot be opened up.
_BRANCH_STATEMENTS: dict[str, frozenset[str]] = {
    "query": frozenset({"list"}),
    "mutate": frozenset({"create", "delete", "merge"}),
}

_BRANCH_STATEMENT_RE = re.compile(r"\A\s*branch\s+([a-z]+)\b")


def _check_branch_statement(verb: str, source: str) -> None:
    """Raise unless ``source`` is a branch statement ``verb`` may carry.

    ★ THIS IS A SECURITY BOUNDARY, NOT A TYPO CHECK. ``statement`` is a public
    write path that takes GQ SOURCE, and witan's secret/PII ``guard`` is
    ``(query_name, params) -> params``: it looks up the named mutation in
    ``scan.enforce.FIELD_MAP`` and rewrites its free-text params. It has no
    way to scan raw source, and a statement has no params to hand it. So an
    arbitrary inline mutation on a guarded client would persist its literals
    without ever being scanned or redacted — witan builds every server client
    with that guard enabled by default.

    Restricting the source to branch statements is what makes the question
    moot rather than merely unlikely: a branch statement's only operand is a
    branch name, so there is no free text for the guard to have missed.
    Refusing only when a guard is installed would be the narrower rule and the
    wrong one — it would leave the shape available on unguarded clients, where
    the next caller inherits the hazard without inheriting the refusal.

    Also folds in the verb check, which the two transports need: ``_http_execute``
    calls a write ``verb == "mutate"`` while ``_WRITE_SUBCOMMANDS`` also holds
    ``load``, ``optimize`` and ``cleanup``. Admitting only the two verbs with a
    statement form is what keeps those definitions in step.
    """
    allowed = _BRANCH_STATEMENTS.get(verb)
    if allowed is None:
        raise ValueError(
            f"statement() runs a query or a mutate, not {verb!r}; "
            "the other subcommands have no inline-statement form."
        )
    match = _BRANCH_STATEMENT_RE.match(source)
    if match is None or match.group(1) not in allowed:
        wanted = ", ".join(f"branch {op}" for op in sorted(allowed))
        raise ValueError(
            f"statement() runs branch statements only, and {verb!r} carries "
            f"{wanted}; got {source[:60]!r}. Arbitrary inline GQ is refused "
            "here because witan's secret/PII write guard scans a named "
            "mutation's params and cannot see raw source — use `change` or "
            "`change_many` with a named query, which it can."
        )


def _strip_line_comments(source: str) -> str:
    """Remove ``//`` line comments, without disturbing a ``//`` inside a string.

    A plain regex substitution can't tell a comment from a URL in a quoted
    value (`"https://example.com"`) and truncates the query body there instead
    of just losing a comment. String-skipping mirrors :func:`_match_delimited`
    exactly, so both stay in step if the `.gq` string-literal syntax ever
    grows an escape this doesn't handle yet.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        if source[i] == '"':
            j = i + 1
            while j < n and source[j] != '"':
                j += 2 if source[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(source[i:j])
            i = j
        elif source.startswith("//", i):
            nl = source.find("\n", i)
            i = n if nl == -1 else nl
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


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
    source = _strip_line_comments(source)
    for match in _QUERY_DECL_RE.finditer(source):
        if match.group(1) != name:
            continue
        params_end = _match_delimited(source, match.end() - 1, "(", ")")
        params = source[match.end() : params_end - 1]
        body_start = source.index("{", params_end)
        body_end = _match_delimited(source, body_start, "{", "}")
        return params.strip(), source[body_start + 1 : body_end - 1].strip()
    raise KeyError(f"query {name!r} not found in source")


def _declared_params(raw_decls: str) -> set[str]:
    """Parameter names a query's declaration list requires."""
    return {
        m.group(1) for d in _split_decls(raw_decls) if (m := _PARAM_REF_RE.search(d))
    }


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
        if missing := _declared_params(raw_decls) - set(params):
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
    #: The HTTP status, when the server answered. HTTP transport only.
    status: int | None = None


def _attempt_result(outcome: _http.Outcome) -> _AttemptResult:
    """One HTTP attempt's outcome, in the shape the retry loop consumes."""
    return _AttemptResult(
        outcome.kind,
        body=outcome.body,
        error=outcome.error,
        retry_after=outcome.retry_after,
        status=outcome.status,
    )


#: The CLI flags a subclass may inject into ``load`` (via ``_extra_args``) and
#: the ``/load/ndjson`` query parameter each one is. witan-code's branched
#: clients inject ``--branch <view> --from main``.
_LOAD_FLAG_PARAMS = {"--branch": "branch", "--from": "from_branch"}


class _CliFailure(NamedTuple):
    #: What to show: stderr, or a rendering of the error object on stdout.
    message: str
    #: What to match the marker tables against: stderr, or the error object
    #: verbatim, so its structured keys (``precondition_failure``,
    #: ``recovery_required``, ``code``) are matched as well as its prose.
    classify: str


def _cli_failure(stdout: str, stderr: str) -> _CliFailure:
    """The text of a failed CLI call, wherever the binary put it.

    On 0.11 every failure is on stderr. On 0.13 a read made with
    ``--format json`` against a server reports its failure as a JSON object on
    STDOUT and leaves stderr empty (measured on 0.13.0: an unknown graph, a
    blocked graph and an unreachable server all do). Classifying stderr alone
    would see nothing, call every one of those FATAL, and raise an error with
    no message in it.

    Stdout is used ONLY when it is such an error object. Anything else there
    is the command's own output (rows a read printed before it was killed,
    say), and matching that against the marker tables would let the text of a
    stored memory decide whether a call is retried or reported as drift.

    The object is rendered the way
    :func:`witan_core.omnigraph_http.error_message` renders the same body off
    the wire, so one condition reads the same on either transport.
    """
    if stderr.strip():
        return _CliFailure(stderr, stderr)
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return _CliFailure(stderr, stderr)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("error"), str):
        return _CliFailure(stderr, stderr)
    status = parsed.get("http_status")
    if isinstance(status, int):
        return _CliFailure(_http.error_message(status, stdout), stdout)
    code = parsed.get("code")
    message = f"{parsed['error']} ({code})" if code else parsed["error"]
    return _CliFailure(message, stdout)


def _classify_cli_error(stderr: str, returncode: int | None = None) -> str:
    """Map an omnigraph CLI failure's text and exit code onto the shared kinds.

    ``stderr`` is :func:`_cli_failure`'s ``classify`` text, which on 0.13 can
    be the JSON error object the binary printed on stdout.
    """
    lowered = stderr.lower()
    # First, because nothing else in a response the CLI itself distrusts
    # should be believed either. See _CLI_CONTRACT_VIOLATION.
    if _CLI_CONTRACT_VIOLATION in lowered:
        return _http.CONTRACT_VIOLATION
    if any(m in lowered for m in _UNAVAILABLE_MARKERS):
        return _http.UNAVAILABLE
    # Ahead of the retryable 503 wordings below: all three are the same status
    # and code, and only this one does not clear by waiting.
    if _http.is_graph_blocked(lowered):
        return _http.FATAL
    if any(m in lowered for m in _GRAPH_NOT_YET_SERVING):
        return _http.UNAVAILABLE
    if returncode == _EXIT_RETRY or any(m in lowered for m in _ADMISSION_CAP_MARKERS):
        return _http.ADMISSION_CAP
    # Ahead of _RETRYABLE deliberately — see _PRECONDITION_FAILED. Terminal
    # beats retryable when a message could be read as either.
    if any(m in lowered for m in _PRECONDITION_FAILED):
        return _http.PRECONDITION_FAILED
    # Ahead of _RECOVERY_REQUIRED and _NEEDS_REPAIR both: this names a sidecar
    # that no amount of waiting resolves and that blocks `omnigraph repair`
    # itself, so either of those readings would retry into the same wall or
    # send the operator to a repair that cannot open the store. See
    # `_http.STORE_QUARANTINED`.
    if _http.is_store_quarantined(lowered):
        return _http.STORE_QUARANTINED
    if any(m in lowered for m in _RECOVERY_REQUIRED):
        return _http.RECOVERY_REQUIRED
    if any(m in lowered for m in _FULL_TEXT_REBUILD_REQUIRED):
        return _http.FULL_TEXT_REBUILD_REQUIRED
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

    def estimate(self, key: str) -> float | None:
        """The measured execution time of a write to ``key``, or ``None``
        before the first. Group commit checks a queued write against it."""
        with self._cv:
            return self._service.get(key)

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


# Same mtime-keyed cache shape as `_QUERY_TEXT_CACHE`, for the declared-vs-
# supplied check `_read_rows` runs on every call — see `_cached_query_text`
# for why an mtime check rather than a plain memoise.
_QUERY_DECLS_CACHE: dict[tuple[str, str, float], set[str]] = {}


def _cached_declared_params(path: Path, query_name: str) -> set[str]:
    key = (str(path), query_name, path.stat().st_mtime)
    declared = _QUERY_DECLS_CACHE.get(key)
    if declared is None:
        raw_decls, _ = parse_query(path.read_text(), query_name)
        declared = _declared_params(raw_decls)
        _QUERY_DECLS_CACHE[key] = declared
    return declared


_QUERY_DESTRUCTIVE_CACHE: dict[tuple[str, str, float], bool] = {}


def _cached_is_destructive(path: Path, query_name: str) -> bool:
    """Whether the named query deletes. omnigraph refuses one query that both
    deletes and inserts or updates, so a group commit must not mix the two."""
    key = (str(path), query_name, path.stat().st_mtime)
    destructive = _QUERY_DESTRUCTIVE_CACHE.get(key)
    if destructive is None:
        _, body = parse_query(path.read_text(), query_name)
        destructive = bool(_DELETE_STATEMENT_RE.search(body))
        _QUERY_DESTRUCTIVE_CACHE[key] = destructive
    return destructive


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
            [
                binary,
                "schema",
                "apply",
                "--schema",
                str(schema_file),
                *store_cli_args(str(store), binary=binary),
            ],
            capture_output=True,
            text=True,
            check=False,
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


def store_address(
    graph_uri: str, graph_id: str | None = None
) -> tuple[str, str | None]:
    """The binary-independent identity of a store: ``(server_url, graph_id)``
    for a deployed server, ``(graph_uri, None)`` for anything opened directly.

    For a caller that needs to ask whether two spellings name the same store
    without rendering CLI flags, whose shape depends on the binary.
    """
    if graph_uri.startswith(_SERVER_SCHEMES):
        return _split_server_uri(graph_uri, graph_id)
    return graph_uri, None


def cli_speaks_targets(binary: str) -> bool:
    """Whether ``binary`` is an omnigraph CLI addressed with 0.14's ``--target``.

    Memoized per binary file: the answer is a property of the executable, and a
    ``witan setup`` that replaces it in place changes its mtime, so the next call
    asks the new one. A binary that cannot be stat'ed or run is answered
    ``False`` so the command itself is what reports it missing, as before.
    """
    # Resolved the way the subprocess will be: a bare command name is found on
    # PATH, not in the working directory.
    resolved = shutil.which(binary)
    if resolved is None:
        return False
    try:
        st = os.stat(resolved)
    except OSError:
        return False
    return _speaks_targets(os.path.realpath(resolved), st.st_mtime_ns, st.st_size)


@functools.cache
def _speaks_targets(binary: str, _mtime_ns: int, _size: int) -> bool:
    try:
        result = subprocess.run(
            [binary, "version", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if result.returncode != 0:
        # Before 0.14, `version` takes no `--json`; 0.13 exits 2 on it.
        return False
    protocol = json.loads(result.stdout)["protocols"]["target"]
    if protocol != _TARGET_PROTOCOL:
        # Upstream: "clients selecting a CLI must require these exact versions;
        # protocol 1 appeared in multiple incompatible edge builds".
        raise RuntimeError(
            f"{binary} speaks omnigraph target protocol {protocol}; witan "
            f"supports {_TARGET_PROTOCOL} (omnigraph 0.14) or a pre-0.14 CLI."
        )
    return True


def _is_loopback(server_url: str) -> bool:
    """The 0.14 CLI's own rule (operator_schema/validation.rs): ``localhost``
    or a loopback IP literal."""
    host = urllib.parse.urlsplit(server_url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _remote_token(token: str | None) -> str | None:
    """The token a remote call presents: the configured one, else the ambient
    ``OMNIGRAPH_BEARER_TOKEN``. One rule for both transports and every binary."""
    return token or os.environ.get(BEARER_TOKEN_ENV_VAR) or None


_operator_dir: Path | None = None
_operator_dir_lock = threading.Lock()


def _is_private_dir(path: Path) -> bool:
    """Ours alone: a real directory, owned by this user, closed to everyone
    else. Nothing but this user (or root) can then create a file inside it."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077
    )


def _operator_config_dir() -> Path:
    """The private directory generated configs live in, re-allocated when lost.

    Never recreated by name. It sits in the shared temp dir, so once a temp
    cleaner removes it the name is free for another local user to claim and
    plant a config whose ``witan`` target would receive this process's actor
    token.
    """
    global _operator_dir
    with _operator_dir_lock:
        if _operator_dir is None or not _is_private_dir(_operator_dir):
            _operator_dir = Path(tempfile.mkdtemp(prefix="witan-omnigraph-"))
            atexit.register(shutil.rmtree, _operator_dir, ignore_errors=True)
        return _operator_dir


def _operator_config(server_url: str | None, *, authenticated: bool) -> str:
    """Path to an operator config naming one server target, or none.

    Every 0.14 call passes one, local ones included: without ``--operator-config``
    the CLI reads ``~/.omnigraph/config.yaml`` and refuses the WHOLE call when
    any entry in it is invalid, so a personal file still in the 0.13 format
    would break witan's local store too.

    JSON is written because it is valid YAML and needs no YAML library. The
    file holds no secret (the token is named by variable), so it is
    content-addressed and shared by every call with the same endpoint. It is
    rewritten if a temp cleaner removed it under a long-lived server, in a
    fresh directory if the cleaner took that too (:func:`_operator_config_dir`).
    """
    doc: dict = {"version": 1}
    if server_url is not None:
        auth = (
            {"provider": "token", "source": {"env": OPERATOR_TOKEN_ENV_VAR}}
            if authenticated
            else {"provider": "anonymous"}
        )
        doc["targets"] = {
            _OPERATOR_TARGET: {"kind": "server", "endpoint": server_url, "auth": auth}
        }
    text = json.dumps(doc, sort_keys=True)
    directory = _operator_config_dir()
    path = directory / f"{hashlib.sha256(text.encode()).hexdigest()[:16]}.yaml"
    if not path.exists():
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    return str(path)


def store_cli_args(
    graph_uri: str,
    graph_id: str | None = None,
    *,
    binary: str,
    token: str | None = None,
) -> list[str]:
    """The CLI flags addressing ``graph_uri`` for ``binary``, for a store with
    no client.

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

    ``binary`` decides the dialect (:func:`cli_speaks_targets`). A pre-0.14 CLI
    takes ``--store <uri>`` or ``--server <url> --graph <id>``. A 0.14 CLI takes
    ``--operator-config <generated> --target <abs path>`` for a local store and
    ``--operator-config <generated> --target witan --graph <id>`` for a server.
    ``token`` (or the ambient one, :func:`_remote_token`) only chooses whether
    that server target authenticates; the value itself goes in the subprocess
    env (:func:`store_subprocess_env`), never in the file.

    Raises ``RuntimeError`` (what witan's callers already report) for what a
    0.14 CLI cannot address at all: a direct ``s3://``/``az://`` store
    (cloud-direct access was removed), and a plain
    ``http://`` server unless it is an anonymous loopback one (the CLI's own
    endpoint rule; it sends a credential only over HTTPS).
    """
    if not cli_speaks_targets(binary):
        if graph_uri.startswith(_SERVER_SCHEMES):
            server_url, resolved = _split_server_uri(graph_uri, graph_id)
            return ["--server", server_url, "--graph", resolved]
        return ["--store", graph_uri]

    if graph_uri.startswith(_SERVER_SCHEMES):
        server_url, resolved = _split_server_uri(graph_uri, graph_id)
        authenticated = _remote_token(token) is not None
        if server_url.startswith("http://") and (
            authenticated or not _is_loopback(server_url)
        ):
            raise RuntimeError(
                f"{server_url}: omnigraph 0.14's CLI reaches a server only over "
                "HTTPS (plain HTTP is allowed for an anonymous loopback server "
                "alone), so it cannot run this command against it. Serve the "
                "graph over https://, or use a pre-0.14 binary."
            )
        config = _operator_config(server_url, authenticated=authenticated)
        return [
            "--operator-config",
            config,
            "--target",
            _OPERATOR_TARGET,
            "--graph",
            resolved,
        ]
    if graph_uri.startswith(_CLOUD_SCHEMES):
        raise RuntimeError(
            f"{graph_uri}: omnigraph 0.14's CLI cannot open an s3:// or az:// "
            "store directly (its inline targets are local paths only); reach it "
            "through an omnigraph-server, or with a pre-0.14 binary."
        )
    if graph_uri.startswith("file://"):
        # A file URI is percent-encoded (`Path.as_uri()` writes a space as
        # %20); a plain path is taken literally, `%` and all.
        graph_uri = urllib.parse.unquote(urllib.parse.urlsplit(graph_uri).path)
    path = os.path.abspath(os.path.expanduser(graph_uri))
    return [
        "--operator-config",
        _operator_config(None, authenticated=False),
        "--target",
        path,
    ]


def init_cli_args(binary: str) -> list[str]:
    """Flags for ``omnigraph init``, which keeps its positional store URI on
    both dialects. 0.14 still reads ``~/.omnigraph/config.yaml`` for it, so it
    gets the same empty generated config as every other local call."""
    if not cli_speaks_targets(binary):
        return []
    return ["--operator-config", _operator_config(None, authenticated=False)]


def write_quiet_args(binary: str) -> list[str]:
    """``--quiet`` for a pre-0.14 write; 0.14 removed the flag (it always prints
    the resolved target to stderr) and refuses it with exit 64."""
    return [] if cli_speaks_targets(binary) else ["--quiet"]


def _aws_profile_env(profile: str) -> dict[str, str]:
    """Resolve one AWS CLI profile into the environment omnigraph understands.

    omnigraph's S3 backend accepts the standard AWS credential environment but
    does not resolve named profiles itself. ``aws configure export-credentials``
    deliberately sits at that boundary: it supports static profiles, SSO,
    ``credential_process``, role assumption, and future AWS CLI providers
    without witan learning or persisting any credential format of its own.
    """
    try:
        result = subprocess.run(
            [
                "aws",
                "configure",
                "export-credentials",
                "--profile",
                profile,
                "--format",
                "process",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"S3 profile {profile!r} requires the AWS CLI, but `aws` was not found."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Could not resolve credentials for S3 profile {profile!r} "
            f"(`aws configure export-credentials` exited {exc.returncode})."
        ) from exc

    try:
        exported = json.loads(result.stdout)
        access_key = exported["AccessKeyId"]
        secret_key = exported["SecretAccessKey"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"AWS CLI returned invalid credentials for S3 profile {profile!r}."
        ) from exc
    if not isinstance(access_key, str) or not isinstance(secret_key, str):
        raise RuntimeError(  # noqa: TRY004 - bad AWS CLI output, not a caller error
            f"AWS CLI returned invalid credentials for S3 profile {profile!r}."
        )

    env = {
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_PROFILE": profile,
        # Explicit credentials should fail as themselves, not pause while the
        # SDK tries an instance-metadata fallback after a profile problem.
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    session_token = exported.get("SessionToken")
    if isinstance(session_token, str) and session_token:
        env["AWS_SESSION_TOKEN"] = session_token
    return env


def store_subprocess_env(
    graph_uri: str,
    token: str | None = None,
    *,
    s3_profile: str | None = None,
    s3_region: str | None = None,
) -> dict:
    """The subprocess environment for an omnigraph CLI call against ``graph_uri``.

    A local path or ``s3://`` root has no bearer-token server, so an ambient
    token is stripped. For an S3 root with ``s3_profile``, the named AWS CLI
    profile is exported to short-lived environment credentials on every
    subprocess call; this keeps SSO/role credentials refreshable without a
    wrapper script or plaintext credentials in witan's config. ``s3_region``
    supplies both standard AWS region spellings. Local and http(s) stores ignore
    both S3 settings. A remote store takes ``token`` when given and otherwise
    the ambient ``OMNIGRAPH_BEARER_TOKEN`` (:func:`_remote_token`).

    The token is set under both names a CLI may read it from, so this needs no
    binary: ``OMNIGRAPH_BEARER_TOKEN`` for a pre-0.14 CLI, and
    ``OPERATOR_TOKEN_ENV_VAR`` for 0.14, whose generated operator config names
    that variable (:func:`store_cli_args`). Each binary ignores the other one.
    """
    env = dict(os.environ)
    resolved = _remote_token(token) if graph_uri.startswith(_SERVER_SCHEMES) else None
    if resolved is None:
        env.pop(BEARER_TOKEN_ENV_VAR, None)
        env.pop(OPERATOR_TOKEN_ENV_VAR, None)
    else:
        env[BEARER_TOKEN_ENV_VAR] = resolved
        env[OPERATOR_TOKEN_ENV_VAR] = resolved

    if graph_uri.startswith("s3://") and s3_profile:
        # Do not combine a freshly exported access/secret pair with an ambient
        # session token from another identity. _aws_profile_env adds the token
        # back when the selected profile actually has one.
        env.pop("AWS_SESSION_TOKEN", None)
        env.pop("AWS_SECURITY_TOKEN", None)
        env.update(_aws_profile_env(s3_profile))
        if s3_region:
            env["AWS_REGION"] = s3_region
            env["AWS_DEFAULT_REGION"] = s3_region
    return env


def _jittered_backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with jitter — plain exponential backoff makes
    concurrent retries from the same burst (the actual trigger case for both
    callers below) retry in lockstep and re-collide; jitter breaks that up."""
    delay = base * (2 ** (attempt - 1))
    jitter = random.uniform(0, 0.1 * delay)
    return min(delay + jitter, cap)


def _conflict_backoff(attempt: int) -> float:
    return random.uniform(
        0, min(_CONFLICT_BASE_DELAY * (2 ** (attempt - 1)), _CONFLICT_MAX_DELAY)
    )


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
# THREE TIERS, NOT TWO, and the middle one is the whole point of this section.
#
#   local path   flock on `<store>.lock`. Excludes every writer on the host,
#                this process's threads and other processes alike.
#   s3://        an in-process mutex keyed by the store URI. There is no local
#                file to flock and no server to arbitrate, but the writers that
#                actually collide are this process's own threads, and those a
#                mutex does exclude.
#   http(s)://   nothing. The writers are on other hosts and the server
#                arbitrates between them; a lock here would only serialise this
#                client against itself for no gain.
#
# ★ THE MIDDLE TIER IS A REVERSAL. s3:// used to sit with http(s) under one
# `_UNLOCKABLE_SCHEMES`, reasoning that a store flock cannot coordinate either
# one. True, and it silently answered a question nobody asked: flock cannot,
# but a mutex can, and "no cross-host lock is possible" is not "no lock is
# worth taking". agent-kit#364 is what that cost — two concurrent tool calls in
# ONE `witan serve` process, against one format-9 direct-S3 root, raced the
# same manifest version and left an OCC recovery sidecar whose delta did not
# match the commit it named. The graph then refused every read, every write and
# `omnigraph repair` until an operator quarantined the sidecar by hand and
# force-repaired four drifted table heads (see StoreQuarantined).
#
# ★ WHAT THIS DOES NOT FIX, SAID PLAINLY: separate processes. Two `witan serve`
# replicas, or a CLI run alongside a server, still write to an s3:// root with
# no coordination between them and can reproduce #364 exactly. A shared s3 root
# wants a served single-writer target (http(s)://) or upstream coordination;
# this closes the one racer pair we control, not the class.
_UNLOCKABLE_SCHEMES = _SERVER_SCHEMES
_IN_PROCESS_LOCK_SCHEMES = ("s3://",)

# Keyed by the store URI, never reclaimed: one entry per graph this process has
# written to, which is bounded by the graphs it serves (16 in production).
_in_process_write_locks: dict[str, threading.RLock] = {}
_in_process_write_locks_guard = threading.Lock()


def _in_process_write_lock(store: str) -> threading.RLock:
    """The process-wide mutex for ``store``, created on first use.

    An ``RLock`` because the nesting the flock tier tracks by hand in
    ``_held_flocks`` — a write issued inside :meth:`hold_write_lock` — has to
    re-enter here too, and an ``RLock`` already does that per thread, which is
    the same keying.
    """
    key = store.rstrip("/")
    with _in_process_write_locks_guard:
        lock = _in_process_write_locks.get(key)
        if lock is None:
            lock = _in_process_write_locks[key] = threading.RLock()
    return lock


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


@contextmanager
def store_write_lock(store: str) -> Iterator[None]:
    """Serialise writes to ``store`` for the duration of the block.

    The single entry point for all three tiers above, so a caller never has to
    know which one a URI falls into. A no-op for a served store.
    """
    if store.startswith(_UNLOCKABLE_SCHEMES):
        yield
        return
    if store.startswith(_IN_PROCESS_LOCK_SCHEMES):
        with _in_process_write_lock(store):
            yield
        return
    # Everything else is a local path, and the fall-through assumes it: a scheme
    # dropped from BOTH lists above would land here and flock `<scheme>:/…lock`,
    # a real local file named after a remote store. Any new scheme belongs in
    # one of the two lists.
    fh = acquire_store_flock(store)
    try:
        yield
    finally:
        release_store_flock(store, fh)


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


class WriteIndeterminate(RuntimeError, Refusal):
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

    # NOT opted in. The raise site appends `\n{err.strip()}`, and `err` is
    # whatever the transport produced: `error_message()` passes a non-JSON HTTP
    # body through verbatim, and the CLI transport hands over the subprocess's
    # whole stderr. Identifiers and advice yes, but not only those.
    log_safe_message = False


class WriteQueueFull(RuntimeError, Refusal):
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

    # Opted in: both raise sites build the message from `label`, `key`
    # (`f"{server_url}|{graph_id}"`) and this process's own counts and times.
    # No upstream string, unlike its neighbours below.
    log_safe_message = True


#: Process-wide, like `_WRITE_GATE`: witan builds a client per request, so lanes
#: kept on a client would group nothing.
_GROUP_COMMITTER = GroupCommitter(OmnigraphConflict, WriteQueueFull)


class WriteContention(RuntimeError, Refusal):
    """A write lost every optimistic-concurrency retry. Nothing was written.

    Each loss is the store refusing the commit because another writer moved
    the table or branch head first ("stale view", "write authority … changed
    during preparation"), which is why re-sending the same mutation is safe
    and why the caller can retry this one too. A refusal rather than a plain
    ``RuntimeError`` because it is load, not a fault: raised bare, it reached
    Sentry as an ERROR on every exhausted retry.
    """

    # NOT opted in: the message ends with the store's own error text.
    log_safe_message = False


class AdmissionCapExceeded(RuntimeError, Refusal):
    """The server's per-actor admission cap refused this write. Nothing was written."""

    # NOT opted in, for the same reason as WriteIndeterminate: both raise
    # sites append `\n{err.strip()}`. Worth knowing that `classify_status`
    # routes ANY 429 here, including an ingress page that never reached
    # omnigraph, so `err` is not even bounded to omnigraph's own vocabulary.
    log_safe_message = False


#: Where an operator hit by :class:`StoreQuarantined` finds the procedure. An
#: absolute URL rather than the repo-relative path, because this message is
#: raised by an installed CLI or MCP client that has no checkout to resolve a
#: relative path against. The page is published by the ``witan-context`` site
#: (``zensical.toml``'s ``site_url``), which serves directory-style URLs.
RUNBOOK_URL = "https://witan-context.readthedocs.io/guides/store-quarantine-runbook/"


class StoreQuarantined(RuntimeError, Refusal):
    """The store will not open: an active OCC recovery sidecar records a manifest
    delta that does not match the original commit it names.

    Every read, every write and ``omnigraph repair`` itself are refused with
    this same error until an operator resolves the sidecar, so there is nothing
    the client can do about it and nothing waiting will fix. Raised on the first
    attempt, for reads as well as writes.

    ★ IT IS A REFUSAL, NOT AN INCIDENT PER CALL. Once a graph is in this state
    every subsequent tool call hits it, and routing each one to Sentry with a
    stack trace buries the single operational fact — one graph is down — under
    hundreds of identical issues. The condition needs one operator, not an
    alert per caller.

    ★ AND IT CARRIES THE OPERATION ID. The remedy needs the sidecar's name to
    quarantine the right file, and the raw error is the only place it appears;
    reading it back out of a truncated log was the slowest step of the
    2026-09-18 recovery. :func:`sidecar_operation_id` parses it out so the
    message can state it and :attr:`operation_id` can carry it.

    See :data:`RUNBOOK_URL` for the recovery procedure.
    """

    # NOT opted in: the raise site appends `\n{err.strip()}` like the two
    # above. The sidecar operation id that an operator actually needs is parsed
    # out and kept on `operation_id`, so it survives this as a structured value
    # even though the prose does not reach the log.
    log_safe_message = False

    def __init__(self, message: str, operation_id: str | None = None) -> None:
        super().__init__(message)
        self.operation_id = operation_id


_SIDECAR_ID_RE = re.compile(r"occ recovery sidecar '([^']+)'", re.IGNORECASE)


def sidecar_operation_id(msg: str) -> str | None:
    """The OCC recovery sidecar's operation id from a quarantine error, if named.

    Lenient like the other response parsers here: a message that does not quote
    an id is still a quarantine, just one whose sidecar the operator has to find
    by listing ``__recovery/``. Failing to parse a detail must never turn a
    legible refusal back into an opaque one.
    """
    match = _SIDECAR_ID_RE.search(msg)
    return match.group(1) if match else None


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
    return _commit_ids_from_mutate_body(body)[0]


def _commit_ids_from_mutate_body(body: str) -> tuple[str | None, str | None]:
    """``(commit.graph_commit_id, commit.parent_commit_id)`` from a raw mutate
    response body. The parent is the head the write replaced, which is how a
    group commit knows a queued write's fence is stale.

    Deliberately lenient rather than raising: an empty body, non-JSON body, or
    a ``"commit": null`` (a write that matched nothing) are all just "no
    commit id available" — the same posture as
    :meth:`OmnigraphClient.read_with_commit` returning ``None``.
    A write itself must never fail because this one piece of BONUS
    information could not be parsed out of an otherwise-successful response.
    """
    if not body.strip():
        return None, None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    commit = parsed.get("commit")
    if not isinstance(commit, dict):
        return None, None
    graph_commit_id = commit.get("graph_commit_id")
    parent_commit_id = commit.get("parent_commit_id")
    return (
        graph_commit_id if isinstance(graph_commit_id, str) else None,
        parent_commit_id if isinstance(parent_commit_id, str) else None,
    )


@contextmanager
def _cli_payload(
    params: dict, source: str | None = None
) -> Iterator[tuple[str, str | None]]:
    """``(params file, source file or None)`` for one CLI query or mutation.

    Passed as files (``--params-file``, ``--query <path>``) rather than argv,
    because Linux caps a single argv element at 128 KiB (``MAX_ARG_STRLEN``):
    a task with a 200 KiB description failed with ``Argument list too long``
    as ``--params``, and a composed batch of 128 ``merge_task`` statements did
    as ``-e``. A private directory under ``TMPDIR``, removed once the call,
    retries included, is done.

    :raises RuntimeError: when the payload cannot be written (``TMPDIR`` full
        or unwritable). Every local call now needs that space, so it is raised
        as the error callers already handle for a CLI that could not run.
    """

    def unwritable(exc: OSError) -> RuntimeError:
        return RuntimeError(
            "omnigraph could not run: its payload could not be written under "
            f"{tempfile.gettempdir()} ({exc}). Point TMPDIR at a writable "
            "directory with free space."
        )

    try:
        tmp = tempfile.TemporaryDirectory(prefix="witan-omnigraph-")
    except OSError as exc:
        raise unwritable(exc) from exc
    with tmp as tmp_dir:
        try:
            params_file = Path(tmp_dir) / "params.json"
            params_file.write_text(json.dumps(params), encoding="utf-8")
            source_file = None
            if source is not None:
                source_file = Path(tmp_dir) / "batch.gq"
                source_file.write_text(source, encoding="utf-8")
        except OSError as exc:
            raise unwritable(exc) from exc
        yield str(params_file), None if source_file is None else str(source_file)


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
        s3_profile: str | None = None,
        s3_region: str | None = None,
    ) -> None:
        self.graph_uri = graph_uri
        self.queries_dir = queries_dir
        self.token = token
        self.guard = guard
        self.s3_profile = s3_profile
        self.s3_region = s3_region
        # Whether a remote call rides out an unreachable server for the full
        # _UNAVAILABLE_MAX_WAIT budget. On by default: that budget exists so an
        # ordinary call survives a server restart instead of failing the whole
        # run. Turn it OFF for a call whose answer *degrades* rather than fails
        # — a listing, an existence check — where waiting 150s to report "no"
        # is strictly worse than reporting it now.
        self.connect_retry = connect_retry
        # http(s) stores are a remote omnigraph-server, addressed by server URL
        # plus graph id; local paths and s3:// roots are opened directly.
        # Resolve the split once at construction so a bad graph id fails fast
        # rather than on the first subprocess.
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

        ``None`` when the response carried none. omnigraph 0.13 reports it on
        both transports whenever the branch has a head commit. A caller must
        not pass ``None`` on as a precondition: ``if_commit=None`` means an
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

        Checks declared-vs-supplied params up front, mirroring the check
        ``compose_batch`` already runs on the write path: a caller omitting a
        declared parameter must get a named ``KeyError`` here rather than
        whatever the engine does with an unbound query variable — on an
        engine predating omnigraph #569 that was a silent, over-broad scan
        rather than an error.
        """
        declared = _cached_declared_params(self.queries_dir / query_file, query_name)
        if missing := declared - set(params):
            msg = f"{query_name} missing parameter(s): {', '.join(sorted(missing))}"
            raise KeyError(msg)
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
            with _cli_payload(params) as (params_file, _):
                result = self._run(
                    "query",
                    "--query",
                    str(self.queries_dir / query_file),
                    query_name,
                    "--params-file",
                    params_file,
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
        # omnigraph 0.11 omits null-valued fields from each row, but `columns`
        # still names every projection. Restoring them as None keeps `row["x"]`
        # meaning what it meant on 0.10 for every caller on both transports.
        if columns := (envelope or {}).get("columns"):
            rows = [{c: None for c in columns} | row for row in rows]
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

        Returns the NEW ``graph_commit_id`` this write produced (the mutate
        response's ``commit.graph_commit_id``; under group commit, the commit
        that carried it with other callers' writes), or ``None`` when there is
        none:
        the CLI path, or a write that matched nothing (0.13 answers those with
        ``"commit": null``). Distinct from ``if_commit``, which is the commit
        you fenced ON before the call. From 0.12 it is the exact commit this
        write published, even when the acknowledgement was lost and the
        server had to read it back. Do not order commit ids as strings: from
        0.12 an id is ``hb1.<block>.<slot>.<nonce>`` with a decimal slot, so
        slot 10 sorts before slot 9.

        ★ HTTP ONLY. The CLI path always returns ``None`` here, DELIBERATELY —
        see the paragraph in the CLI branch below before trying to close that
        gap with ``--json``.
        """
        if self.guard is not None:
            params = self.guard(query_name, params)
        if self._groups_writes():
            return self._submit_to_group(
                [(query_file, query_name, params)],
                surface_conflict=surface_conflict,
                if_commit=if_commit,
            )
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
        # classified from `result.stderr` only at the time, so adding --json
        # here would have starved that classifier on every CLI-path
        # precondition failure. It reads an error object off stdout now
        # (`_cli_failure`), but only the read shapes were measured that way on
        # 0.13, not a `mutate --json`. The CLI path stays without a returned
        # commit id rather than risk that.
        extra = ["--if-commit", if_commit] if if_commit is not None else []
        with _cli_payload(params) as (params_file, _):
            self._run(
                "mutate",
                "--query",
                str(self.queries_dir / query_file),
                query_name,
                "--params-file",
                params_file,
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
        if_commit: str | None = None,
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

        ``if_commit`` fences the whole batch the way it fences :meth:`change`:
        the commit applies only while that branch head is still current, and
        a refusal means no step was applied. A conflict cannot be attributed to
        one step, so a compare-and-swap caller re-reads everything it derived
        the batch from. It cannot be combined with ``chunk_size``: chunks are
        separate commits, and only the first could state the head it was read
        at.

        ``chunk_size`` splits the work into that many statements per commit and
        must be >= 1.
        The composed query is one request body over HTTP, so a caller with an
        unbounded number of steps (a repo reindex, a store-wide backfill) must
        cap it or eventually exceed the server's payload limit. It is
        opt-in because it TRADES AWAY ATOMICITY: chunks commit independently and
        a failure part-way leaves the earlier ones applied. A chunk failure is
        re-raised naming its position, the batch size and the elapsed time, so
        the caller can tell how much landed without re-deriving it — bounded by
        the caveat that the FAILING chunk's own fate is unknown when the failure
        is mid-flight, so the count is a lower bound and not a resume point.

        A single step is passed straight through to ``change`` rather than
        wrapped: the composed form would be equivalent, but the direct path
        keeps the named query in the error message and skips the splice.

        Against a deployed server both this and :meth:`change` go through the
        process-wide group committer, so the commit may also carry other
        callers' writes (see :mod:`witan_core.group_commit`). The outcome
        reported is still this call's own.
        """
        if not steps:
            return
        if chunk_size is not None and chunk_size < 1:
            # Not a redundant type check: `range(0, n, -1)` is EMPTY, so a
            # negative size would skip the loop, return normally, and silently
            # drop every write. Zero raises, but as an opaque "range() arg 3
            # must not be zero" far from the caller that chose the value.
            raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
        if chunk_size is not None and if_commit is not None:
            raise ValueError("if_commit cannot fence a chunked batch")
        if chunk_size is not None and len(steps) > chunk_size:
            chunks = math.ceil(len(steps) / chunk_size)
            for index, start in enumerate(range(0, len(steps), chunk_size), start=1):
                batch = steps[start : start + chunk_size]
                started = time.monotonic()
                try:
                    self.change_many(batch, surface_conflict=surface_conflict)
                except Exception as exc:
                    # Chunks commit independently, so WHICH chunk failed is the
                    # difference between "the write is slow" and "the write is
                    # broken". The bare error names neither the position nor the
                    # batch size that produced it, which is exactly what a
                    # timeout needs you to know.
                    #
                    # ★ THE PRIOR CHUNKS ARE A LOWER BOUND, NOT THE STATE. This
                    # chunk's own fate is genuinely unknown when the failure is
                    # a mid-flight timeout: `omnigraph_http._send` classifies
                    # exactly that case as possibly-committed, which is why it
                    # refuses to retry a non-idempotent write there. Reporting
                    # "chunks 1-N committed" as if it were the resulting state
                    # would invite a caller to resume at N+1 and silently
                    # re-apply this one.
                    elapsed = time.monotonic() - started
                    landed = (
                        f"chunks 1-{index - 1} committed"
                        if index > 1
                        else "no chunk committed"
                    )
                    raise RuntimeError(
                        f"chunk {index}/{chunks} of a {len(steps)}-statement "
                        f"batch failed after {elapsed:.1f}s "
                        f"({len(batch)} statements, chunk_size={chunk_size}; "
                        f"{landed} before it, and this chunk may or may not "
                        f"have): {exc}"
                    ) from exc
            return
        if len(steps) == 1:
            query_file, name, params = steps[0]
            self.change(
                query_file,
                name,
                params,
                surface_conflict=surface_conflict,
                if_commit=if_commit,
            )
            return
        if self.guard is not None:
            steps = [(f, n, self.guard(n, p)) for f, n, p in steps]
        if self._groups_writes():
            self._submit_to_group(
                steps, surface_conflict=surface_conflict, if_commit=if_commit
            )
            return
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
                if_graph_commit=if_commit,
            )
            return
        extra = ["--if-commit", if_commit] if if_commit is not None else []
        with _cli_payload(params, source) as (params_file, source_file):
            self._run(
                "mutate",
                "--query",
                source_file,
                "witan_batch",
                "--params-file",
                params_file,
                *extra,
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
        stays large until they are GC'd. ``cleanup`` removes the versions no
        retained graph commit pins. From omnigraph 0.12 ``keep`` retains the
        newest N graph commits of every live branch (0.11 counted versions per
        table), and ``older_than`` retains every commit newer than a Go-style
        duration like ``7d``. A commit survives when either retains it. At
        least one bound must be given (omnigraph requires it). ``--confirm``
        is passed so it runs.
        """
        if keep is None and older_than is None:
            raise ValueError("cleanup requires keep and/or older_than")
        args = ["--confirm"]
        if keep is not None:
            args += ["--keep", str(keep)]
        if older_than is not None:
            args += ["--older-than", older_than]
        return self._run("cleanup", *args)

    def statement(
        self,
        verb: str,
        source: str,
        *cli_args: str,
        label: str | None = None,
        surface_conflict: bool = False,
    ) -> str:
        """Run one inline GQ statement — no query file, no name, no params.

        omnigraph 0.11 exposes branch work as GQ over the canonical routes
        (upstream RFC 0055): ``branch list`` is a ``query`` statement and
        ``branch create``/``delete``/``merge`` are ``mutate`` statements. That
        is what this exists for, and it is the whole reason branch work no
        longer needs a second subprocess shape: a statement rides the same
        transport, the same retry/admission policy and the same error
        classification as every other read and write.

        ★ THE ROUTE DOES NOT WIDEN AUTHORITY, which is the thing to check
        before putting a branch delete on the same endpoint as every ordinary
        write. A WRITE statement is authorized as its branch action, not as
        the route's. Measured against a 0.11.0 server running witan's own
        code-graph Cedar bundle (2026-09-21): a ``witan-users`` actor, who
        holds ``change`` on unprotected branches but deliberately not
        ``branch_delete``, gets ``403 policy denied action 'branch_delete'
        targeting branch 'act-alice/wip'`` from ``POST /mutate``, while the
        same request as ``act-svc-witan-ci`` succeeds. ``branch create``
        likewise checks ``branch_create``.

        READS DO NOT FOLLOW THAT RULE, and the asymmetry is load-bearing.
        ``branch list`` on ``/query`` checks plain ``read``, exactly as a named
        query does: an actor in no group is refused with ``policy denied action
        'read'``, the same wording an ordinary read gets. Had it checked a
        ``branch_list`` of its own, every read-only actor would start failing
        ``witan_code.store.probe_cluster_graph``, which exists precisely
        because listing one graph's branches needs nothing beyond ``read``.

        THREE THINGS DIFFER FROM A NAMED CALL, all of them load-bearing:

        - **No params key on the wire.** Not an empty one — none. See
          :meth:`~witan_core.omnigraph_http.PooledTransport.query`.
        - **No ``_extra_args``.** A statement takes no ``--branch``, so the
          subclass args are neither injected nor lost, which is also what lets
          a branched client use HTTP here (``carries_extra_args=False``).
        - **``cli_args`` reach the CLI path only.** They are the flags with no
          body equivalent — ``--format json`` to get a parseable read,
          ``--yes`` for a destructive write against a non-local scope — and the
          HTTP path needs neither, since it always answers JSON and carries its
          authorization in the token.

        ``label`` names the operation in an error message and in the retry
        loop's logging, defaulting to ``verb``. A statement's verb is a poor
        label on its own: every branch operation would report itself as
        ``omnigraph mutate failed``, where the CLI subcommand this replaces
        said ``branch``.

        ★ ``source`` MUST BE A BRANCH STATEMENT, and that is a security
        boundary rather than a convenience: this is a public write path taking
        GQ source, and witan's secret/PII ``guard`` can only scan a named
        mutation's params. See :func:`_check_branch_statement`.

        ``surface_conflict`` matters more here than it does for
        :meth:`change`, because 0.11 answers a duplicate ``branch create``
        with **HTTP 409** (verified against the 0.11.0 server: ``{"error":
        "branch 'x' already exists", "code": "conflict"}``). ``classify_status``
        reads a 409 as retryable on purpose, so without this an
        already-exists — a routine outcome when two callers create the same
        view at once — would be retried through the whole budget, with its
        backoff sleeps, before surfacing. The CLI path has no such trap: the
        same failure is prose that matches no marker and classifies FATAL.
        """
        _check_branch_statement(verb, source)
        is_write = verb == "mutate"
        transport = self._http_transport(carries_extra_args=False)
        if transport is not None:
            return self._http_execute(
                transport,
                verb,
                source,
                None,
                label or verb,
                surface_conflict=surface_conflict,
            )
        cmd = [
            self._binary,
            verb,
            *self._store_args(),
            *(write_quiet_args(self._binary) if is_write else []),
            "-e",
            source,
            *cli_args,
        ]
        return self._execute(
            cmd, label or verb, is_write=is_write, surface_conflict=surface_conflict
        )

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

    def _http_transport(
        self, *, carries_extra_args: bool = True
    ) -> _http.PooledTransport | None:
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

        ``carries_extra_args=False`` switches condition 3 off. The condition
        exists because an injected arg would be DROPPED on the HTTP body; a
        call that injects none, or carries its own on the wire, has nothing to
        drop. Three callers pass it:

        - :meth:`statement`. omnigraph 0.11's branch statements take "no name,
          params, --branch or --snapshot" (the CLI's own help), so a branched
          client may run one over HTTP without its ``--branch`` going missing,
          because there was never a ``--branch`` to send.
        - :meth:`export_to`. Its CLI form never took ``_extra_args`` either
          (it is built from ``_store_args`` alone), so it exports ``main`` on
          both transports.
        - :meth:`load_batch`, which DOES have a branch to target and carries
          it as the route's query parameters (see :meth:`_load_target`), and
          refuses any injected flag it cannot carry.

        Do not reuse this for anything else that has a branch to target
        without carrying it the same way.

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
        if carries_extra_args and (
            self._extra_args("query") or self._extra_args("mutate")
        ):
            return None
        return shared_transport(self.server_url)

    def _resolve_token(self) -> str | None:
        """The bearer token to present over HTTP.

        The same token the CLI subprocess is handed (:func:`_remote_token`),
        and the fallback is the load-bearing half: a remote client with no
        configured token deliberately inherits an ambient
        ``OMNIGRAPH_BEARER_TOKEN`` (a pre-0.14 CLI's own last resort, which
        witan keeps reading for both transports, and a supported way to drive a
        remote graph). Sending only
        ``self.token`` would silently drop that and turn a working setup into
        unauthenticated 401s — the same class of failure as the
        wrong-variable-name bug this env var is named for.

        Read per call rather than cached at construction so the two transports
        resolve it at the same moment, and so a test or a process that exports
        the variable late behaves the same either way.
        """
        return _remote_token(self.token)

    def _query_source(self, query_file: str, query_name: str) -> str:
        """The named query's text, for the HTTP body (which takes it inline)."""
        path = self.queries_dir / query_file
        return _cached_query_text(path, query_name)

    def _http_execute(
        self,
        transport: _http.PooledTransport,
        verb: str,
        source: str,
        params: dict | None,
        label: str,
        *,
        surface_conflict: bool = False,
        if_graph_commit: str | None = None,
        started: float | None = None,
        attempts: list[_AttemptResult] | None = None,
    ) -> str:
        """Run one query/mutate over HTTP under the shared retry policy.

        ``started`` moves the start of the call budget earlier, for a group
        commit whose members queued before it was sent. ``attempts`` collects
        each attempt's result, so a group commit can tell a definite rejection
        from a failure whose effects are unknown.

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
            result = _attempt_result(outcome)
            if attempts is not None:
                attempts.append(result)
            return result

        return self._with_retry_policy(
            attempt,
            label,
            is_write=is_write,
            surface_conflict=surface_conflict,
            started=started,
        )

    # ── Group commit (remote writes over HTTP) ──

    def _groups_writes(self) -> bool:
        """Whether this client's writes go through the group committer.

        Only remote writes over the HTTP transport. A local or s3 store
        serializes on a lock that :meth:`hold_write_lock` re-enters on the
        caller's own thread, so a commit led from another thread would wait on
        it forever. And the CLI reports a failed write as text, which cannot
        tell a definite rejection (safe to replay one write at a time) from a
        lost response (not safe).
        """
        if os.environ.get(GROUP_COMMIT_ENV_VAR, "").strip().lower() in _FALSEY:
            return False
        return self._http_transport() is not None

    def _submit_to_group(
        self,
        steps: list[Step],
        *,
        surface_conflict: bool,
        if_commit: str | None,
    ) -> str | None:
        """Queue ``steps`` on this actor's lane for this graph and wait for them.

        The lane key is everything that changes what the request does or who
        it is from. The token is in it because omnigraph authorizes a request
        once and records one actor on its commit, so a commit must never carry
        two actors' writes. It is hashed so the lane table holds no secret.
        """
        token = self._resolve_token()
        key = (
            type(self),
            self.server_url,
            self.graph_id,
            hashlib.sha256(token.encode()).hexdigest() if token else None,
            str(self.queries_dir),
            self.connect_retry,
        )
        gate_key = self._write_gate_key()
        return _GROUP_COMMITTER.submit(
            key,
            steps,
            self._commit_group,
            if_commit=if_commit,
            surface_conflict=surface_conflict,
            destructive=any(
                _cached_is_destructive(self.queries_dir / query_file, name)
                for query_file, name, _ in steps
            ),
            cost=sum(len(json.dumps(params, default=str)) for _, _, params in steps),
            budget=_env_override(REMOTE_CALL_BUDGET_ENV_VAR, _REMOTE_CALL_BUDGET, float)
            or None,
            # Only a lane's leader enters the write gate, so a queued write is
            # held to the gate's queue limit and service estimate here.
            queue_wait=_env_override(
                REMOTE_WRITE_QUEUE_WAIT_ENV_VAR, _REMOTE_WRITE_QUEUE_WAIT, float
            ),
            estimate=lambda: _WRITE_GATE.estimate(gate_key),
        )

    def _write_gate_key(self) -> str:
        """This graph's key in :data:`_WRITE_GATE`."""
        return f"{self.server_url}|{self.graph_id}"

    def _commit_group(
        self,
        steps: list[Step],
        if_commit: str | None,
        surface_conflict: bool,
        started: float,
    ) -> tuple[str | None, str | None]:
        """Commit one group as one mutate. A lone step is sent as its own named
        query, so an unloaded graph sees exactly the request it saw before."""
        transport = self._http_transport()
        if transport is None:
            raise RuntimeError("group commit needs the HTTP transport")
        if len(steps) == 1:
            query_file, query_name, params = steps[0]
            source = self._query_source(query_file, query_name)
        else:
            sources: dict[str, str] = {}

            def read_source(query_file: str) -> str:
                if query_file not in sources:
                    sources[query_file] = (self.queries_dir / query_file).read_text()
                return sources[query_file]

            try:
                source, params = compose_batch(steps, read_source)
            except KeyError as exc:
                # One member's missing parameter, caught before anything was
                # sent: replaying one write at a time gives it to that member.
                raise RejectedBatch(exc) from exc
        attempts: list[_AttemptResult] = []
        try:
            body = self._http_execute(
                transport,
                "mutate",
                source,
                params,
                "mutate",
                surface_conflict=surface_conflict,
                if_graph_commit=if_commit,
                started=started,
                attempts=attempts,
            )
        except OmnigraphConflict as exc:
            raise ConditionFailed(exc) from exc
        except RuntimeError as exc:
            # Exactly RuntimeError: every subclass (unavailable, contention,
            # indeterminate, queue full, quarantined) is a condition of the
            # graph, not of one caller's statements, and replaying cannot help.
            last = attempts[-1] if attempts else None
            if type(exc) is not RuntimeError or last is None:
                raise
            if last.status == _PRECONDITION_FAILED_STATUS:
                # The fence of a caller that does not surface conflicts.
                raise ConditionFailed(exc) from exc
            if last.status not in _DEFINITE_REJECTION_STATUSES:
                raise
            if last.error.lstrip().startswith(_DATAFUSION_ERROR_PREFIX):
                # omnigraph marks this one completion-uncertain and closes
                # write admission on it, so the write may have landed.
                raise WriteIndeterminate(
                    "omnigraph mutate failed with a query error the server "
                    "treats as completion-uncertain. ITS OUTCOME IS "
                    "INDETERMINATE: the write may or may not have been "
                    f"applied. Re-read before retrying:\n{last.error.strip()}"
                ) from exc
            raise RejectedBatch(exc) from exc
        return _commit_ids_from_mutate_body(body)

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

        A deployed server is exported over the pooled HTTP transport
        (``POST /graphs/<id>/export``), streamed to ``path`` the same way. The
        subprocess stays for local and ``s3://`` stores, and for a remote one
        only behind :data:`HTTP_TRANSPORT_ENV_VAR`: the 0.14 CLI refuses a
        plain-http server target, which is what the deployed data tier is.
        """
        env = self._subprocess_env()
        out = Path(path)
        transport = self._http_transport(carries_extra_args=False)
        if transport is not None:
            token = self._resolve_token()

            def http_attempt() -> _AttemptResult:
                # "wb", truncating per attempt for the reason the CLI attempt
                # below does: a retry must not append to a failed prefix.
                try:
                    with open(out, "wb") as fh:
                        return _attempt_result(
                            transport.export(self.graph_id, fh, token)
                        )
                except OSError as exc:
                    raise RuntimeError(
                        f"omnigraph {label} could not write {out}: {exc}"
                    ) from exc

            self._with_retry_policy(http_attempt, label, is_write=False)
            return

        cmd = [self._binary, "export", *self._store_args()]

        def attempt() -> _AttemptResult:
            # Truncate per attempt ("w", not "a"): a retry must overwrite
            # whatever the failed attempt already streamed, or a server that
            # dies mid-export leaves a prefix that the successful retry then
            # appends a second full copy onto.
            try:
                with open(out, "w", encoding="utf-8") as fh:
                    result = subprocess.run(
                        cmd,
                        stdout=fh,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=env,
                        check=False,
                    )
            except OSError as exc:
                raise RuntimeError(f"omnigraph {label} could not run: {exc}") from exc
            if result.returncode == 0:
                return _AttemptResult(_http.OK, returncode=0)
            return _AttemptResult(
                _classify_cli_error(result.stderr, result.returncode),
                error=result.stderr,
                returncode=result.returncode,
            )

        # is_write=False: an export takes no write lock, and its retries are
        # unconditionally safe because repeating a read changes nothing.
        self._with_retry_policy(attempt, label, is_write=False)

    def load_batch(self, records: list[dict], mode: str = "merge") -> str:
        """Bulk-load one batch of node/edge records in one ``omnigraph load``.

        Returns the load summary (empty for an empty batch), which
        ``merge_store`` reports back to the user: the CLI's stdout for a local
        store, or the server's ``GraphBatchLoadOutput`` JSON for a deployed
        one. Most callers ignore it.

        A deployed server is loaded over the pooled HTTP transport
        (``POST /graphs/<id>/load/ndjson``), with the same NDJSON as the body
        that the CLI would have posted from its ``--data`` file. The subprocess
        stays for local and ``s3://`` stores, and for a remote one only behind
        :data:`HTTP_TRANSPORT_ENV_VAR`: the 0.14 CLI refuses a plain-http
        server target, which is what the deployed data tier is.

        Each record is a JSONL line: ``{"type": Node, "data": {...}}`` for a
        node or ``{"edge": Edge, "from": key, "to": key}`` for an edge. One
        load replaces a ``mutate`` per record.

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
        transport = self._http_transport(carries_extra_args=False)
        if transport is not None:
            target = self._load_target()
            data = "".join(json.dumps(record) + "\n" for record in records)
            token = self._resolve_token()

            def attempt() -> _AttemptResult:
                return _attempt_result(
                    transport.load_ndjson(
                        self.graph_id, data, token, mode=mode, **target
                    )
                )

            return self._with_retry_policy(attempt, "load", is_write=True)
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

    def _load_target(self) -> dict[str, str]:
        """``_extra_args("load")`` as :meth:`PooledTransport.load_ndjson` keywords.

        The HTTP load has to target what the CLI load would have, and the only
        record of that is the flags a subclass injects. Each one maps to a
        query parameter of the route (``_LOAD_FLAG_PARAMS``); a flag with no
        mapping is refused rather than dropped, because dropping ``--branch``
        is the WIP-reindex-into-``main`` failure ``_http_transport`` exists to
        prevent.
        """
        args = self._extra_args("load")
        target: dict[str, str] = {}
        for flag, value in zip(args[::2], args[1::2], strict=True):
            if flag not in _LOAD_FLAG_PARAMS:
                raise RuntimeError(
                    f"{type(self).__name__} injects {flag!r} into `load`, which "
                    "has no /load/ndjson query parameter; refusing to drop it"
                )
            target[_LOAD_FLAG_PARAMS[flag]] = value
        return target

    def _store_args(self) -> list[str]:
        """The CLI flags that address this store with this client's binary;
        :func:`store_cli_args` has the per-dialect forms.

        Passes the already-resolved ``graph_id`` rather than re-deriving it, so
        a client built from a bare server URL plus ``WITAN_MEMORY_GRAPH`` keeps
        addressing the same graph.
        """
        return store_cli_args(
            self.graph_uri, self.graph_id, binary=self._binary, token=self.token
        )

    def _run(self, subcommand: str, *args: str, surface_conflict: bool = False) -> str:
        is_write = subcommand in _WRITE_SUBCOMMANDS
        quiet = write_quiet_args(self._binary) if is_write else []
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
            is_write=is_write,
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
        retry loop for optimistic-concurrency conflicts."""
        env = self._subprocess_env()

        def attempt() -> _AttemptResult:
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, env=env, check=False
                )
            except OSError as exc:
                raise RuntimeError(f"omnigraph {label} could not run: {exc}") from exc
            if result.returncode == 0:
                return _AttemptResult(_http.OK, body=result.stdout, returncode=0)
            failure = _cli_failure(result.stdout, result.stderr)
            return _AttemptResult(
                _classify_cli_error(failure.classify, result.returncode),
                error=failure.message,
                returncode=result.returncode,
            )

        return self._with_retry_policy(
            attempt,
            label,
            is_write=is_write,
            surface_conflict=surface_conflict,
        )

    def _subprocess_env(self) -> dict:
        # A local path or an s3:// root is opened directly — there is no server
        # to present a bearer token to, and s3 authenticates with AWS
        # credentials instead, so an ambient token is stripped rather than
        # merely not set. A remote store with no explicit token deliberately
        # takes the ambient one (see BEARER_TOKEN_ENV_VAR), and `export
        # OMNIGRAPH_BEARER_TOKEN=…` with no token in config is a supported way
        # to drive it. Both rules live in `store_subprocess_env`.
        return store_subprocess_env(
            self.graph_uri,
            self.token,
            s3_profile=self.s3_profile,
            s3_region=self.s3_region,
        )

    def _with_retry_policy(
        self,
        attempt_once: Callable[[], _AttemptResult],
        label: str,
        *,
        is_write: bool,
        surface_conflict: bool = False,
        started: float | None = None,
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

        * ``load_batch`` shelled out to ``omnigraph load`` (it had no HTTP form
          then, and still does not behind :data:`HTTP_TRANSPORT_ENV_VAR`), so
          every batch of a ``store_merge`` bypassed the bound. Two people
          migrating at once is precisely the multi-user burst this exists to
          bound.
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
        origin = time.monotonic() if started is None else started
        deadline = origin + budget if budget else None
        gate = (
            _WRITE_GATE.admit(self._write_gate_key(), label, call_deadline=deadline)
            if gate_write
            else nullcontext()
        )
        # The store lock goes INSIDE the admission gate, exactly where the
        # flock it generalises used to sit: a write must not hold the store
        # against this process's other threads while it waits for a server slot
        # it may never be given.
        write_lock = store_write_lock(self.graph_uri) if is_write else nullcontext()
        with gate, write_lock:
            return self._retry_loop(
                attempt_once,
                label,
                is_write=is_write,
                surface_conflict=surface_conflict,
                deadline=deadline,
            )

    def _retry_loop(
        self,
        attempt_once: Callable[[], _AttemptResult],
        label: str,
        *,
        is_write: bool,
        surface_conflict: bool,
        deadline: float | None,
    ) -> str:
        """The loop itself, inside whatever admission :meth:`_with_retry_policy`
        decided on. ``deadline`` is a ``time.monotonic()`` stamp past which this
        call cannot usefully still be running, or ``None`` when no cut-off is
        known — in which case a ``Retry-After`` is obeyed rather than
        second-guessed."""
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
                remaining = None if deadline is None else deadline - time.monotonic()
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
                    raise AdmissionCapExceeded(
                        f"omnigraph {label} was refused by the server's "
                        f"admission cap, {asked} — longer than the "
                        f"{max(remaining, 0.0):.1f}s this call has left. "
                        f"Nothing was written; retry in {delay:.0f}s:\n"
                        f"{err.strip()}"
                    )
                if admission_cap_attempt < _ADMISSION_CAP_MAX_ATTEMPTS:
                    time.sleep(delay)
                    continue
                raise AdmissionCapExceeded(
                    f"omnigraph {label} failed after "
                    f"{_ADMISSION_CAP_MAX_ATTEMPTS} attempts (actor "
                    f"admission cap exceeded):\n{err.strip()}"
                )
            if kind == _http.STORE_QUARANTINED:
                # TERMINAL FOR READS TOO, and on the first attempt. The
                # sidecar stays active until an operator moves it, so the
                # retry budget only spends the caller's deadline before
                # reporting the same thing.
                operation_id = sidecar_operation_id(err)
                named = f" (sidecar operation {operation_id})" if operation_id else ""
                raise StoreQuarantined(
                    f"omnigraph {label} refused: the store is quarantined by "
                    f"an unresolved OCC recovery sidecar{named}. Reads, "
                    f"writes and `omnigraph repair` are all blocked until an "
                    f"operator resolves it — see {RUNBOOK_URL}. Retrying "
                    f"will not clear it:\n{err.strip()}",
                    operation_id,
                ) from None
            if kind == _http.CONTRACT_VIOLATION and is_write:
                # The response did not carry exactly the stamp of the
                # omnigraph-server we speak to (none, another contract's, or
                # more than one), so it says nothing about whether the write
                # was applied. A read falls through to the RuntimeError below.
                raise WriteIndeterminate(
                    f"omnigraph {label} got a response that did not carry "
                    f"exactly the expected HTTP contract stamp. ITS OUTCOME IS "
                    f"INDETERMINATE: the "
                    f"write may or may not have been applied. Re-read before "
                    f"retrying:\n{err.strip()}"
                ) from None
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
            if kind == _http.FULL_TEXT_REBUILD_REQUIRED:
                # TERMINAL, on the first attempt. The index cannot serve
                # this engine's analyzer and no amount of retrying changes
                # that; upstream says so outright. Ordinary reads are
                # unaffected — only full-text queries land here — so this is
                # not a broken graph, and the remedy is in the server's own
                # message, which is why it is passed through whole.
                raise RuntimeError(
                    f"omnigraph {label} was refused because a full-text "
                    f"index needs an explicit rebuild for this engine "
                    f"version. Retrying will not clear it, and ordinary "
                    f"(non-search) reads are unaffected:\n{err.strip()}"
                )
            if kind == _http.NEEDS_REPAIR:
                # TERMINAL, on the first attempt: see _NEEDS_REPAIR. The
                # store's own message names the dataset and both versions,
                # so it is passed through whole.
                raise RuntimeError(
                    f"omnigraph {label} was refused because a dataset's Lance "
                    f"HEAD has moved ahead of the version the graph manifest "
                    f"publishes. Retrying will not clear it, and this client "
                    f"cannot reconcile it: an operator has to run "
                    f"`omnigraph repair --json` against the store to see "
                    f"which datasets drifted and whether the movement is "
                    f"maintenance or a foreign write:\n{err.strip()}"
                )
            if surface_conflict and kind == _http.RETRYABLE:
                # A compare-and-swap caller wants to lose the race, not
                # re-apply its write over the winner. Surface immediately.
                raise OmnigraphConflict(err.strip()) from None
            attempt += 1
            if attempt < _MAX_ATTEMPTS and kind == _http.RETRYABLE:
                delay = _conflict_backoff(attempt)
                # A retry that starts after the caller's cut-off can only end
                # as a torn-down connection, the indeterminate write the
                # admission-cap branch above refuses to risk too.
                if deadline is None or time.monotonic() + delay < deadline:
                    time.sleep(delay)
                    continue
            # `exit N` only makes sense for a subprocess; an HTTP attempt
            # carries no returncode and says so by omitting it, rather than
            # inventing one that would read as a CLI exit status.
            exited = "" if result.returncode is None else f" (exit {result.returncode})"
            if kind == _http.RETRYABLE and is_write:
                raise WriteContention(
                    f"omnigraph {label} lost {attempt} optimistic-"
                    f"concurrency races in a row{exited} — another writer kept "
                    f"committing to the same tables. Nothing was written; "
                    f"retry once the other writer is done:\n{err.strip()}"
                ) from None
            raise RuntimeError(f"omnigraph {label} failed{exited}:\n{err.strip()}")

    @contextmanager
    def hold_write_lock(self) -> Iterator[None]:
        """Hold this store's write lock across several operations.

        For a caller whose *sequence* has to be atomic, not just each step —
        ``merge_store`` exports the target, reconciles against it, then loads
        the winners, and another writer landing between the export and the load
        would make the decisions stale.

        The nested writes inside the block re-enter the lock rather than
        blocking on it — see :func:`acquire_store_flock` for the flock tier and
        :func:`_in_process_write_lock` for the s3 one. A no-op for a served
        store, where the server arbitrates between writers this client cannot
        see.
        """
        with store_write_lock(self.graph_uri):
            yield

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
