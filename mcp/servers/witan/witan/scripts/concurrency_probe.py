"""Observe how a DEPLOYED witan behaves under concurrent clients.

Phase-exit criterion 2 of wp-witan-multi-user-service-deployment-dcf6ee asks for
concurrency to be *observed*, not argued from the design. The design argument is
weak on purpose: ``acquire_store_flock`` is skipped for http(s) stores
(``_UNLOCKABLE_SCHEMES``), so the advisory lock that serialises local writers
does nothing here, and ``task_claim`` is documented best-effort CAS pending an
upstream omnigraph conditional-write.

Three probes, each run by N genuinely independent OS processes -- separate
interpreters, separate MCP clients, separate connections -- released against the
deployment at one wall-clock instant:

  A  mutual-exclusion   N workers race ``task_claim`` on one task.
                        PASS: exactly one ``claimed: true``; every loser gives a
                        structured refusal rather than an error.
  B  no-lost-writes     N workers ``memory_store`` distinct memories at once.
                        PASS: all N readable afterwards. Sharper than A because
                        ``_execute`` masks OCC conflicts by re-running the
                        mutation, which has never been watched under real
                        contention on a shared store.
  C  read-availability  Readers run against the deployment while B's writes are
                        in flight. PASS: every read returns, none errors.

★ EVERY probe additionally requires that the concurrency it claims to test
  ACTUALLY HAPPENED, and fails otherwise. This is not ceremony -- it is the
  difference between evidence and a green tick. A run whose workers fired
  seconds apart measured a queue; readers that ran after the last write
  measured an idle server; one racer never contends. Each of those once
  produced a confident PASS. So:

    - A and B fail unless every worker made the epoch and the fire spread is
      within --spread-tolerance-ms.
    - C fails unless every clean read overlapped the writers' window.
    - --racers/--writers below 2, or --readers below 1, are rejected outright.

  A probe that cannot certify it observed contention reports FAIL, loudly,
  with the reason -- never a PASS that means nothing.

Run against a configured target (see docs/deployed-witan-onboarding.md)::

    witan login --target ci
    uv run python -m witan.scripts.concurrency_probe --target ci

It writes real rows to the target graph. Everything it creates is tagged
``concurrency-probe`` and the scratch task is closed on the way out; ``--keep``
leaves them for inspection.

── WHAT IT FOUND, 2026-08-07, against witan.ci.ol.mit.edu ──
Recorded here so a later run has something to differ from:

  A  held at 3, 8 and 16 racers, every time: exactly one ``claimed: true``,
     every loser a structured refusal, zero errors. Note the refusals came back
     as ``held`` rather than ``lost_race`` -- the winner's write landed before
     the losers' *read*, so the best-effort CAS retry loop was not itself
     exercised even at 16-way contention.
  B  zero lost writes in every run. At 16 writers, 4-5 were rejected outright
     at connect; every write the server ACKED was readable afterwards. It fails
     closed, which is the safe direction.
  C  clean to 8 readers, degrades above -- same connect-level rejection as B.

The rejections are not the store and not authentication. Both are separately
filed: the deployment saturates at ~24-32 concurrent connections and the
ingress answers 502 rather than a graceful 429
(tk-deployed-witan-saturates-at-24-32-concurrent-con-8e4afc), and N concurrent
clients stampede the shared OIDC token cache
(tk-concurrent-agents-stampede-the-oidc-token-refres-677984) -- which is why
this probe pins one token across its workers instead of letting each refresh.

── AND AGAIN, 2026-08-09, after the vMCP memory fix (ol-infrastructure #5320) ──
The 2026-08-07 ceiling was the vMCP being OOMKilled. That is gone: across every
run below, the vMCP, proxy runner and backend each held `restartCount 0` with an
empty `lastState`, while the vMCP's own log recorded ~150 sessions opened and
terminated in one 9-minute window. The same load previously killed it outright.

  A  4 and 16 racers: exactly one `claimed: true`, every loser a structured
     `held` refusal, zero errors -- unchanged.
  B  12 writers: 12 acked, 12/12 readable, zero lost, zero duplicated slugs.
  C  6 readers alongside those writers: all clean, all overlapping the window.

  24 writers + 12 readers still FAILS, but for a different reason than before
  and NOT by crashing anything: ~half the workers came back RemoteUnreachable
  wrapping `error_code=-32603`, while every pod's restart count stayed at 0.

★ THE NUMBERS IN THE PARAGRAPH BELOW ARE NOT THIS PROBE'S. Read them as a
  separate experiment, because that is what they are. This probe issues ONE
  `memory_store` per writer and, per reader, `n_reads` sequential `task_get`s
  -- so a 24+12 run contains 24 concurrent writes and at most 12 simultaneous
  reads (72 in total), never 36 of either. The counts below come from a
  raw-HTTP burst harness: N independent connections, each doing one MCP
  `initialize` plus one `tools/call` of a chosen tool, so that "36 concurrent
  writes" means exactly that and the answer is a status code rather than a
  client exception. It is not in this repo; reproduce it with any N-way
  client that posts to /mcp directly.

  Against witan.ci.ol.mit.edu, all fired within a 6ms spread:
      36 x memory_store   -> {200: 11, 502: 25}, the 502s an APISIX HTML page
      36 x memory_search  -> {200: 36}, p50 15.8s against 4.4s for N=1
  At the same instant the proxy runner logged exactly 25 x `http: proxy error:
  context canceled` and the backend logged only 13 completions -- so writes are
  queueing until a deadline above the backend cancels them. Reads degrade;
  writes fail. Filed as its own task; the write ceiling is now latency, not
  memory.

  ★ AND CHECK THE BODY, NOT THE STATUS, if you rebuild that harness: a wrong
    tool name comes back as HTTP 200 carrying a JSON-RPC error, which reads as
    a clean pass. The deployed vMCP exposes tools UNPREFIXED (`memory_store`,
    not `witan_memory_store`); an early run of the above "passed" at N=64 while
    calling a tool that does not exist.

★ A LOCAL CEILING TO KNOW ABOUT: past some worker count this probe stops being
  able to measure the server at all, because each worker is a separate
  interpreter that must import and connect before the epoch. Observed on one
  laptop: 18 workers made the default 20s lead comfortably; 36 did not, and the
  run reported `22 LATE, fire spread 28062ms` and correctly refused to call
  itself a race. 75s was enough for 36 there. The number is per-machine -- read
  that FAIL as "raise --lead", not as a verdict on the server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import cyclopts

app = cyclopts.App(
    name="concurrency-probe",
    help="Observe a deployed witan under concurrent clients.",
)

#: Workers busy-wait on a shared epoch instead of a barrier the parent controls,
#: because each one is a separate process with its own MCP handshake. The delay
#: has to cover the slowest worker's import + connect + tool-list, or the
#: "concurrent" calls arrive staggered and the probe proves nothing.
DEFAULT_LEAD_SECONDS = 20.0

#: How long one worker may take before the parent kills it. A worker does ONE
#: tool call after the epoch, so this is a hang guard, not a work budget --
#: which is why it is far below the ~5 minute Keycloak access token it has to
#: fit inside. See _pinned_token: the token must cover a whole phase
#: (lead + this + margin), and a 300s guard made that impossible to satisfy.
WORKER_TIMEOUT_S = 90.0

#: A "concurrent" call that lands 2 seconds after its peers measured a queue,
#: not a race. Every probe therefore gates its own PASS on the workers having
#: actually fired together; this is that gate, in milliseconds.
#:
#: ★ The default is a STARTING POINT, not a measured constant. Tighten it once
#: a few live runs show what this machine's spread really is -- too loose and a
#: staggered run passes vacuously, which is the exact failure the gate exists
#: to prevent. Override with --spread-tolerance-ms.
DEFAULT_SPREAD_TOLERANCE_MS = 500.0


def _srv(target: str, token: str | None = None):
    """Build the same remote proxy the CLI dispatches through.

    ``token`` pins a pre-fetched access token instead of letting each process
    consult the shared token cache. That is not a convenience: the proxy calls
    its token provider on EVERY invoke, so without pinning, N workers firing at
    one instant stampede the OIDC refresh and fail on the cache race
    (tk-concurrent-agents-stampede-the-oidc-token-refres-677984) -- and this
    probe would measure that bug instead of the store it is aimed at.
    """
    os.environ["WITAN_TARGET"] = target
    from witan import config as cfg_module
    from witan.remote.oidc import default_token_provider
    from witan.remote.proxy import RemoteServerProxy

    remote = cfg_module.load_remote_config()
    if remote is None:
        raise SystemExit(
            f"target {target!r} is not a remote target -- this probe only means "
            "something against a deployment (see [targets.*] in witan config)"
        )
    provider = (lambda: token) if token else default_token_provider(remote)
    return RemoteServerProxy(remote, provider)


def _pinned_token(target: str, needed_s: float) -> tuple[str, float]:
    """A token guaranteed to outlive the run, plus its remaining lifetime.

    The plain provider returns whatever is cached as long as it has more than
    ``_EXPIRY_SKEW_S`` (30s) left -- so pinning it naively can hand every worker
    a token with 31 seconds to live, and a probe with a 55-second lead then
    measures 401s rather than the store. Force a refresh when the cached token
    cannot cover the whole run.
    """
    import httpx2

    from witan import config as cfg_module
    from witan.remote.oidc import _auth, decode_claims, default_token_provider

    cfg = cfg_module.load_remote_config()
    token = default_token_provider(cfg)()
    remaining = decode_claims(token).get("exp", 0) - time.time()
    if remaining < needed_s:
        auth = _auth(cfg)
        entry = auth._load_cache().get(auth._cache_key()) or {}
        if not entry.get("refresh_token"):
            raise SystemExit(
                f"cached token has {remaining:.0f}s left, the run needs "
                f"{needed_s:.0f}s, and there is no refresh token -- run "
                f"`witan login --target {target}` and retry"
            )
        with httpx2.Client(timeout=15) as client:
            token = auth._refresh(entry["refresh_token"], client)["access_token"]
        remaining = decode_claims(token).get("exp", 0) - time.time()
        if remaining < needed_s:
            raise SystemExit(
                f"even a freshly-refreshed token only has {remaining:.0f}s, but "
                f"this run needs {needed_s:.0f}s -- lower --lead or --racers"
            )
    return token, remaining


# ── worker ───────────────────────────────────────────────────────────────────
#
# Re-entered as a subprocess. Emits exactly one JSON line so the parent can read
# results without sharing memory.


def _worker(mode: str, index: int, start_at: float, payload: dict) -> None:
    out: dict[str, Any] = {"index": index, "mode": mode}
    try:
        srv = _srv(payload["target"], payload.get("token"))
        # Pay the connect + tool-list cost BEFORE the barrier; otherwise the
        # first call of each worker races the handshake, not the store.
        srv.task_get(slug=payload["warmup_slug"])
        out["ready_at"] = time.time()

        while time.time() < start_at:
            time.sleep(0.001)

        fired = time.time()
        if mode == "claim":
            result = srv.task_claim(
                slug=payload["slug"], assignee=f"probe-worker-{index}"
            )
        elif mode == "store":
            result = srv.memory_store(
                kind="agent_context",
                title=f"{payload['label']} writer {index}",
                content=(
                    f"Concurrency probe row {index} of run {payload['run_id']}. "
                    "Written to observe whether concurrent writers to the shared "
                    "deployed graph lose updates. Safe to delete."
                ),
                repo=payload["repo"],
                tags=["concurrency-probe", payload["run_id"]],
            )
        elif mode == "read":
            reads = [
                srv.task_get(slug=payload["slug"]) for _ in range(payload["n_reads"])
            ]
            result = {
                "reads": len(reads),
                "all_returned": all(r is not None for r in reads),
            }
        else:  # pragma: no cover - guarded by the caller
            raise ValueError(f"unknown mode {mode!r}")

        out["fired_at"] = fired
        out["done_at"] = time.time()
        out["ok"] = True
        out["result"] = result
    except BaseException as exc:  # noqa: BLE001 - the failure IS the observation
        out["ok"] = False
        out["error_type"] = type(exc).__name__
        out["error"] = str(exc)[:400]
        out.update(_error_detail(exc))
    print(json.dumps(out), flush=True)


def _error_detail(exc: BaseException) -> dict[str, Any]:
    """Everything about a failure that ``str(exc)`` throws away.

    ``MCPError.__str__`` is its ``message`` alone, and the message the deployed
    stack produces under load is the useless sentence "Server returned an error
    response" -- which named neither the layer nor the status and cost a whole
    session to chase. The JSON-RPC ``code``/``data`` and the causal chain are
    what distinguish a server-side refusal from a transport failure, so record
    them rather than the sentence.

    The traversal is ``witan_core.remote.proxy._chain``, the same walk the proxy
    classifies faults with -- every group member AND every cause/context, not
    the first branch of each. Rolling a cheaper one here meant a coded error
    sitting in a group's second member, or under the group's own ``__cause__``,
    was invisible: exactly the reachable-but-unread failure this helper exists
    to stop happening.
    """
    from witan_core.remote.proxy import _chain

    detail: dict[str, Any] = {}
    chain = []
    for link in _chain(exc):
        chain.append(f"{type(link).__name__}: {str(link)[:120]}")
        # Read the code/status off whichever link CARRIES it, not off the
        # outermost exception: witan wraps the transport fault in a
        # RemoteUnreachable whose prose is all the reader ever saw, and which
        # has no code of its own. First one wins -- _chain yields outermost
        # first, so the nearest cause is preferred over a deeper one.
        code = getattr(link, "code", None)
        if isinstance(code, int) and "error_code" not in detail:
            detail["error_code"] = code
        data = getattr(link, "data", None)
        if data is not None and "error_data" not in detail:
            detail["error_data"] = str(data)[:400]
        status = getattr(getattr(link, "response", None), "status_code", None)
        if status is not None and "http_status" not in detail:
            detail["http_status"] = status
    if len(chain) > 1:
        detail["error_chain"] = chain[:6]
    return detail


@dataclass
class Outcome:
    """What one probe saw, in counts rather than adjectives."""

    name: str
    passed: bool
    detail: str
    rows: list[dict] = field(default_factory=list)


def _launch(specs: list[tuple[str, int, dict]], start_at: float) -> list[dict]:
    """Start every worker at once and collect one JSON row each, in order.

    One launcher for all three probes. The writers and readers of B/C used to
    have their own copy, which drifted: it never handled a timeout at all.
    """
    procs = []
    for mode, index, payload in specs:
        proc = subprocess.Popen(  # noqa: S603 - argv is ours, payload is on stdin
            [
                sys.executable,
                os.path.abspath(__file__),
                "worker",
                "--mode",
                mode,
                "--index",
                str(index),
                "--start-at",
                repr(start_at),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Hand the payload over on stdin, NOW.
        #
        # Not in argv: it carries the pinned bearer token, and argv is world
        # readable in `ps` and /proc/*/cmdline for this process's whole life --
        # which, because workers deliberately idle until the shared epoch, is
        # the entire lead interval plus the run.
        #
        # And not at collection time: every worker has to be free to connect
        # and warm up immediately. Writing each payload just before we read
        # that worker's output would serialise the warmups behind one another
        # and destroy the very simultaneity the probe is trying to create.
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
        procs.append((mode, index, proc))

    rows = []
    for mode, index, proc in procs:
        try:
            stdout, stderr = proc.communicate(timeout=WORKER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # Kill and REAP it. Letting the exception escape aborted the whole
            # probe and left the worker running against the live deployment,
            # still writing rows nobody was collecting.
            proc.kill()
            stdout, stderr = proc.communicate()
            rows.append(
                {
                    "index": index,
                    "mode": mode,
                    "ok": False,
                    "error_type": "TimeoutExpired",
                    "error": f"exceeded {WORKER_TIMEOUT_S:.0f}s; killed",
                }
            )
            continue
        line = next(
            (ln for ln in reversed(stdout.splitlines()) if ln.startswith("{")), None
        )
        if line is None:
            rows.append(
                {
                    "index": index,
                    "mode": mode,
                    "ok": False,
                    "error_type": "NoResult",
                    "error": f"no result line; stderr={stderr[-300:]}",
                }
            )
        else:
            row = json.loads(line)
            row["mode"] = mode
            rows.append(row)
    return rows


@dataclass
class Timing:
    """Whether the workers actually raced, as opposed to merely running.

    Every probe's PASS is gated on this. Without it all three could report
    success on a run where the workers fired seconds apart -- a queue, not
    contention -- which is precisely the vacuous green this whole script exists
    to avoid producing.
    """

    late: list[int]
    spread_ms: float | None
    n_fired: int

    def raced(self, tolerance_ms: float) -> bool:
        return (
            not self.late
            and self.n_fired > 1
            and self.spread_ms is not None
            and self.spread_ms <= tolerance_ms
        )

    def why_not(self, tolerance_ms: float) -> str:
        if self.late:
            return f"workers {self.late} missed the epoch (warmup ran past it)"
        if self.n_fired < 2:
            return f"only {self.n_fired} worker(s) fired at all"
        return f"fire spread {self.spread_ms:.0f}ms > {tolerance_ms:.0f}ms tolerance"

    def describe(self) -> str:
        if self.spread_ms is None:
            return "fire spread n/a"
        late = f", {len(self.late)} LATE" if self.late else ""
        return f"fire spread {self.spread_ms:.0f}ms{late}"


def _timing(rows: list[dict], start_at: float) -> Timing:
    """Measure simultaneity from the workers' own clocks.

    A worker whose warmup finished *after* the epoch never waited on the busy
    loop at all -- it fired the moment it was ready. It is not part of the race
    and its lateness invalidates the run, so it is tracked separately from the
    spread rather than silently widening it.
    """
    late = [
        r.get("index")
        for r in rows
        if r.get("ready_at") is not None and r["ready_at"] > start_at
    ]
    fired = [r["fired_at"] for r in rows if r.get("fired_at")]
    spread = (max(fired) - min(fired)) * 1000 if len(fired) > 1 else None
    return Timing(late=late, spread_ms=spread, n_fired=len(fired))


# ── probe A: mutual exclusion ────────────────────────────────────────────────


def probe_mutual_exclusion(srv, target, n, lead, run_id, token, spread_tol):
    task = srv.task_create(
        title=f"[concurrency-probe {run_id}] scratch claim target",
        description=(
            "Scratch task created by the concurrency probe to race task_claim "
            "against. Safe to close or delete."
        ),
        priority="p3",
        type="chore",
        tags=["concurrency-probe", run_id],
    )
    slug = task["slug"]
    payload = {
        "target": target,
        "slug": slug,
        "warmup_slug": slug,
        "run_id": run_id,
        "token": token,
    }
    start_at = time.time() + lead
    rows = _launch([("claim", i, payload) for i in range(n)], start_at)
    timing = _timing(rows, start_at)

    winners, losers, malformed = [], [], []
    for row in rows:
        if not row.get("ok"):
            continue
        result = row.get("result") or {}
        if result.get("claimed"):
            winners.append(row)
        elif result.get("claimed") is False and str(result.get("reason") or "").strip():
            losers.append(row)
        else:
            # Not a refusal -- an unreadable answer. `claimed: null`, a missing
            # result, or a blank reason says nothing about whether mutual
            # exclusion held, and counting it as a well-behaved loser let a
            # single real winner carry the whole probe to PASS.
            malformed.append(row)
    errored = [r for r in rows if not r.get("ok")]
    reasons: dict[str, int] = {}
    for row in losers:
        key = (row["result"] or {}).get("reason", "?")
        reasons[key] = reasons.get(key, 0) + 1

    # Exactly one winner is the whole point. Zero is also a failure: it would
    # mean contention can make a claimable task unclaimable by everyone. And
    # none of it means anything unless the racers actually raced.
    passed = (
        len(winners) == 1 and not errored and not malformed and timing.raced(spread_tol)
    )
    detail = (
        f"{n} racers -> {len(winners)} claimed, {len(losers)} refused "
        f"({reasons or 'none'}), {len(malformed)} malformed, "
        f"{len(errored)} errored; {timing.describe()}"
    )
    if not timing.raced(spread_tol):
        detail += f"\n      NOT A RACE: {timing.why_not(spread_tol)}"
    if malformed:
        detail += f"\n      MALFORMED (ok, but neither a claim nor a refusal): {[r.get('index') for r in malformed]}"
    return Outcome(
        name="A mutual-exclusion", passed=passed, detail=detail, rows=rows
    ), slug


# ── probes B + C: lost writes, and reads under that write load ───────────────


def probe_writes_and_reads(
    srv,
    target,
    n_writers,
    n_readers,
    lead,
    run_id,
    scratch_slug,
    repo,
    token,
    spread_tol,
    slug_sink,
):
    start_at = time.time() + lead
    payload_w = {
        "target": target,
        "warmup_slug": scratch_slug,
        "run_id": run_id,
        "label": f"[concurrency-probe {run_id}]",
        "repo": repo,
        "token": token,
    }
    payload_r = {
        "target": target,
        "warmup_slug": scratch_slug,
        "run_id": run_id,
        "slug": scratch_slug,
        "n_reads": 6,
        "token": token,
    }

    # Writers and readers must be in flight together, so they are launched as one
    # batch rather than by two sequential _spawn calls.
    specs = [("store", i, payload_w) for i in range(n_writers)]
    specs += [("read", i, payload_r) for i in range(n_readers)]
    rows = _launch(specs, start_at)
    write_rows = [r for r in rows if r.get("mode") == "store"]
    read_rows = [r for r in rows if r.get("mode") == "read"]
    write_timing = _timing(write_rows, start_at)

    # B: a write the server ACKNOWLEDGED must be readable. A worker that got a
    # slug back and whose row is then absent is a lost update -- the failure
    # this probe exists to catch.
    acked = [
        r for r in write_rows if r.get("ok") and (r.get("result") or {}).get("slug")
    ]
    # A worker that returned successfully but produced no slug fell through
    # BOTH lists -- not acked, not errored -- so B could report PASS having
    # verified fewer rows than it launched writers. There is no benign reading
    # of "the store said ok and named nothing"; it is an error.
    no_slug = [
        r for r in write_rows if r.get("ok") and not (r.get("result") or {}).get("slug")
    ]
    slugs = [(r["result"]["slug"], r["index"]) for r in acked]
    # Record what exists in the graph BEFORE verifying it, so an exception in
    # verification still leaves the caller able to clean these up.
    slug_sink.extend(s for s, _ in slugs)
    # A verification read that RAISES proves nothing about the write -- it is
    # inconclusive, and calling it a lost update (as an earlier revision did)
    # manufactures alarming false positives whenever the service is briefly
    # unhappy. Retry, then classify absent-vs-unknown separately.
    missing, present, unknown = [], [], []
    for slug, idx in slugs:
        got, err = None, None
        for attempt in range(4):
            try:
                got, err = srv.memory_get(slug=slug), None
                break
            except BaseException as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {str(exc)[:120]}"
                time.sleep(2 * (attempt + 1))
        if err is not None:
            unknown.append((slug, idx, err))
        elif got:
            present.append((slug, idx))
        else:
            missing.append((slug, idx, "absent"))
    dupes = len(slugs) - len({s for s, _ in slugs})
    write_errors = [r for r in write_rows if not r.get("ok")]

    b = Outcome(
        name="B no-lost-writes",
        # Only a row the server ACKED and that is then verifiably ABSENT is a
        # lost write. Unverifiable reads make the probe inconclusive, not failed.
        # Simultaneity is a precondition: writes that did not overlap cannot
        # have contended, so their not being lost proves nothing.
        passed=(
            not missing
            and not dupes
            and not write_errors
            and not unknown
            and not no_slug
            and write_timing.raced(spread_tol)
        ),
        detail=(
            f"{n_writers} concurrent writers -> {len(acked)} acked, "
            f"{len(write_errors)} errored, {len(no_slug)} ok-but-no-slug, "
            f"{len(present)}/{len(acked)} readable "
            f"afterwards, {len(missing)} LOST, {len(unknown)} unverifiable, "
            f"{dupes} slug collisions; {write_timing.describe()}"
        ),
        rows=write_rows,
    )
    if not write_timing.raced(spread_tol):
        b.detail += f"\n      NOT CONCURRENT: {write_timing.why_not(spread_tol)}"
    if no_slug:
        b.detail += (
            f"\n      NO SLUG (server said ok, named nothing): "
            f"{[r.get('index') for r in no_slug]}"
        )
    if missing:
        b.detail += f"\n      LOST (acked then absent): {missing}"
    if unknown:
        b.detail += (
            f"\n      UNVERIFIABLE (read failed, write may be fine): {unknown[:3]}"
        )

    read_ok = [
        r
        for r in read_rows
        if r.get("ok") and (r.get("result") or {}).get("all_returned")
    ]
    read_bad = [r for r in read_rows if r not in read_ok]

    # C's claim is "reads stayed clean WHILE writes were in flight" -- so the
    # reads have to have actually been in flight at the same time. A reader
    # that started after the last write finished is measuring an idle server,
    # and counting it as clean is how this probe would certify availability
    # under a load it never applied.
    fired = [r["fired_at"] for r in write_rows if r.get("fired_at")]
    done = [r["done_at"] for r in write_rows if r.get("done_at")]
    window = (min(fired), max(done)) if fired and done else None
    overlapping, disjoint = [], []
    for row in read_ok:
        if window is None or not (row.get("fired_at") and row.get("done_at")):
            disjoint.append(row)
        elif row["fired_at"] <= window[1] and row["done_at"] >= window[0]:
            overlapping.append(row)
        else:
            disjoint.append(row)

    c = Outcome(
        name="C read-availability",
        passed=bool(overlapping) and not read_bad and not disjoint,
        detail=(
            f"{n_readers} readers x {payload_r['n_reads']} reads during the write "
            f"storm -> {len(read_ok)} clean, {len(read_bad)} degraded, "
            f"{len(overlapping)}/{len(read_ok)} overlapped the write window"
            + (
                f"; errors={[r.get('error_type') or r.get('error') for r in read_bad][:3]}"
                if read_bad
                else ""
            )
        ),
        rows=read_rows,
    )
    if disjoint:
        c.detail += (
            f"\n      NOT UNDER LOAD: readers {[r.get('index') for r in disjoint]} "
            "ran outside the write window; their success says nothing about "
            "availability under concurrent writes"
        )
    elif not overlapping:
        c.detail += "\n      NOT UNDER LOAD: no read overlapped any write"
    return b, c


@app.command
def worker(*, mode: str, index: int, start_at: float) -> None:
    """Internal: one concurrent client. Not meant to be run by hand.

    The payload arrives on **stdin**, not in argv, because it carries the
    pinned bearer token -- see the note in :func:`_launch`.
    """
    _worker(mode, index, start_at, json.loads(sys.stdin.read()))


def _cleanup(srv, task_slug: str | None, mem_slugs: list[str], run_id: str) -> None:
    """Best-effort removal of everything the run created. Never raises."""
    print("-" * 72)
    if task_slug:
        try:
            srv.task_close(
                slug=task_slug, resolution=f"concurrency probe {run_id} complete"
            )
        except BaseException as exc:  # noqa: BLE001
            print(f"  cleanup: task {task_slug} left open ({type(exc).__name__})")
    cleaned, refused = 0, []
    for slug in mem_slugs:
        try:
            result = srv.memory_delete(slug=slug, confirm=True)
        except BaseException as exc:  # noqa: BLE001
            print(f"  cleanup: {slug} left behind ({type(exc).__name__})")
            continue
        # memory_delete REFUSES by returning {"deleted": False, "reason": ...} --
        # an author mismatch does not raise. Counting every non-raising return
        # as cleaned made the closing line claim rows were gone while they were
        # still sitting in the shared graph.
        if (result or {}).get("deleted"):
            cleaned += 1
        else:
            refused.append((slug, (result or {}).get("reason", "?")))
    for slug, reason in refused:
        print(f"  cleanup: {slug} NOT deleted ({reason})")
    print(f"cleaned up scratch task + {cleaned}/{len(mem_slugs)} probe memories")


@app.default
def run(
    *,
    target: str = "ci",
    racers: int = 8,
    writers: int = 8,
    readers: int = 4,
    lead: float = DEFAULT_LEAD_SECONDS,
    repo: str = "https://github.com/mitodl/agent-kit",
    keep: bool = False,
    spread_tolerance_ms: float = DEFAULT_SPREAD_TOLERANCE_MS,
) -> None:
    """Run all three probes against a deployed target and report counts.

    Parameters
    ----------
    target: named target from the witan config (must be a remote one).
    racers: concurrent clients racing one task_claim (probe A). Minimum 2.
    writers: concurrent clients writing distinct memories (probe B). Minimum 2.
    readers: concurrent clients reading during those writes (probe C). Minimum 1.
    lead: seconds each worker gets to connect before the synchronised fire.
    repo: repo key written on the probe's memories.
    keep: leave the probe's rows in the graph instead of cleaning up.
    spread_tolerance_ms: how far apart the workers may fire and still count as
        concurrent. See DEFAULT_SPREAD_TOLERANCE_MS -- the default is a
        starting point to calibrate, not a measured constant.
    """
    # Reject counts that cannot demonstrate anything, rather than reporting a
    # confident PASS over them: one racer never contends, zero writers never
    # write, zero readers never read. Each of those was a green run that
    # observed nothing at all.
    if racers < 2:
        raise SystemExit("--racers must be >= 2; a single racer cannot contend")
    if writers < 2:
        raise SystemExit("--writers must be >= 2; a single writer cannot conflict")
    if readers < 1:
        raise SystemExit("--readers must be >= 1; probe C needs a reader")
    if lead <= 0:
        raise SystemExit("--lead must be > 0; workers need time to connect first")
    if spread_tolerance_ms <= 0:
        raise SystemExit("--spread-tolerance-ms must be > 0")

    run_id = f"probe-{uuid.uuid4().hex[:8]}"
    os.environ["WITAN_TARGET"] = target
    # The PARENT keeps the normal refreshing provider: it is single-threaded, so
    # it cannot stampede, and its setup/verify/cleanup calls outlive any one
    # token's ~5min lifetime. Only the concurrent workers get a pinned one.
    srv = _srv(target)
    print(f"target={target} run={run_id}")
    print("-" * 72)

    scratch_slug: str | None = None
    mem_slugs: list[str] = []
    try:
        # A token PER PHASE, not one for the whole run. A Keycloak access token
        # lives ~5 minutes; one run may legitimately span longer than that once
        # each phase is allowed its full worker timeout, and a phase that fires
        # with an expired token measures 401s rather than the store. Re-pinning
        # keeps the requirement to a single phase, which is satisfiable.
        phase_s = lead + WORKER_TIMEOUT_S + 30
        token, remaining = _pinned_token(target, needed_s=phase_s)
        print(
            f"probe A: token pinned, {remaining:.0f}s of life for a {phase_s:.0f}s phase"
        )
        a, scratch_slug = probe_mutual_exclusion(
            srv, target, racers, lead, run_id, token, spread_tolerance_ms
        )

        token, remaining = _pinned_token(target, needed_s=phase_s)
        print(
            f"probe B/C: token pinned, {remaining:.0f}s of life for a {phase_s:.0f}s phase"
        )
        b, c = probe_writes_and_reads(
            srv,
            target,
            writers,
            readers,
            lead,
            run_id,
            scratch_slug,
            repo,
            token,
            spread_tolerance_ms,
            mem_slugs,
        )

        for outcome in (a, b, c):
            print(
                f"[{'PASS' if outcome.passed else 'FAIL'}] "
                f"{outcome.name}: {outcome.detail}"
            )
            for row in outcome.rows:
                if not row.get("ok") or not (row.get("result") or {}).get(
                    "all_returned", True
                ):
                    print(
                        f"      worker {row.get('index')}: "
                        f"{row.get('error_type')}: "
                        f"{row.get('error') or row.get('result')}"
                    )
                    for key in ("error_code", "http_status", "error_data"):
                        if row.get(key) is not None:
                            print(f"          {key}={row[key]}")
                    for link in row.get("error_chain") or []:
                        print(f"          via {link}")
        failed = [o.name for o in (a, b, c) if not o.passed]
    finally:
        # In a finally, because everything above writes REAL rows to a SHARED
        # graph. A parent-side timeout, a JSON decode failure, a raise out of
        # verification, or a ^C used to skip cleanup entirely and leave probe
        # rows behind for everyone else to trip over.
        if not keep:
            _cleanup(srv, scratch_slug, mem_slugs, run_id)

    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    app()
