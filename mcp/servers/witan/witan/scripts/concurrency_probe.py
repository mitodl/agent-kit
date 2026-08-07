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
    print(json.dumps(out), flush=True)


@dataclass
class Outcome:
    """What one probe saw, in counts rather than adjectives."""

    name: str
    passed: bool
    detail: str
    rows: list[dict] = field(default_factory=list)


def _spawn(mode: str, n: int, payload: dict, lead: float) -> list[dict]:
    """Run ``n`` workers concurrently and collect their JSON lines."""
    start_at = time.time() + lead
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                os.path.abspath(__file__),
                "worker",
                "--mode",
                mode,
                "--index",
                str(i),
                "--start-at",
                repr(start_at),
                "--payload",
                json.dumps(payload),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(n)
    ]
    rows = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=payload.get("timeout", 300))
        line = next(
            (ln for ln in reversed(stdout.splitlines()) if ln.startswith("{")), None
        )
        if line is None:
            rows.append(
                {"ok": False, "error": f"no result line; stderr={stderr[-300:]}"}
            )
        else:
            rows.append(json.loads(line))
    return rows


def _spread_ms(rows: list[dict]) -> float | None:
    """How tightly the workers actually fired. A wide spread means the probe
    measured a queue, not a race, and the result should not be trusted."""
    fired = [r["fired_at"] for r in rows if r.get("fired_at")]
    return (max(fired) - min(fired)) * 1000 if len(fired) > 1 else None


# ── probe A: mutual exclusion ────────────────────────────────────────────────


def probe_mutual_exclusion(srv, target, n, lead, run_id, token):
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
    rows = _spawn(
        "claim",
        n,
        {
            "target": target,
            "slug": slug,
            "warmup_slug": slug,
            "run_id": run_id,
            "token": token,
        },
        lead,
    )

    winners = [
        r for r in rows if r.get("ok") and (r.get("result") or {}).get("claimed")
    ]
    losers = [
        r for r in rows if r.get("ok") and not (r.get("result") or {}).get("claimed")
    ]
    errored = [r for r in rows if not r.get("ok")]
    reasons: dict[str, int] = {}
    for row in losers:
        reasons[(row["result"] or {}).get("reason", "?")] = (
            reasons.get((row["result"] or {}).get("reason", "?"), 0) + 1
        )

    # Exactly one winner is the whole point. Zero is also a failure: it would
    # mean contention can make a claimable task unclaimable by everyone.
    passed = len(winners) == 1 and not errored
    return Outcome(
        name="A mutual-exclusion",
        passed=passed,
        detail=(
            f"{n} racers -> {len(winners)} claimed, {len(losers)} refused "
            f"({reasons or 'none'}), {len(errored)} errored; "
            f"fire spread {_spread_ms(rows):.0f}ms"
            if _spread_ms(rows) is not None
            else f"{n} racers -> {len(winners)} claimed"
        ),
        rows=rows,
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
    procs = [
        (
            mode,
            subprocess.Popen(
                [
                    sys.executable,
                    os.path.abspath(__file__),
                    "worker",
                    "--mode",
                    mode,
                    "--index",
                    str(i),
                    "--start-at",
                    repr(start_at),
                    "--payload",
                    json.dumps(pl),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ),
        )
        for mode, i, pl in specs
    ]
    write_rows, read_rows = [], []
    for mode, proc in procs:
        stdout, stderr = proc.communicate(timeout=300)
        line = next(
            (ln for ln in reversed(stdout.splitlines()) if ln.startswith("{")), None
        )
        row = (
            json.loads(line)
            if line
            else {"ok": False, "error": f"no result; {stderr[-300:]}"}
        )
        (write_rows if mode == "store" else read_rows).append(row)

    # B: a write the server ACKNOWLEDGED must be readable. A worker that got a
    # slug back and whose row is then absent is a lost update -- the failure
    # this probe exists to catch.
    acked = [
        r for r in write_rows if r.get("ok") and (r.get("result") or {}).get("slug")
    ]
    slugs = [(r["result"]["slug"], r["index"]) for r in acked]
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
        passed=not missing and not dupes and not write_errors and not unknown,
        detail=(
            f"{n_writers} concurrent writers -> {len(acked)} acked, "
            f"{len(write_errors)} errored, {len(present)}/{len(acked)} readable "
            f"afterwards, {len(missing)} LOST, {len(unknown)} unverifiable, "
            f"{dupes} slug collisions"
            + (
                f"; fire spread {_spread_ms(write_rows):.0f}ms"
                if _spread_ms(write_rows)
                else ""
            )
        ),
        rows=write_rows,
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
    c = Outcome(
        name="C read-availability",
        passed=not read_bad,
        detail=(
            f"{n_readers} readers x {payload_r['n_reads']} reads during the write "
            f"storm -> {len(read_ok)} clean, {len(read_bad)} degraded"
            + (
                f"; errors={[r.get('error_type') or r.get('error') for r in read_bad][:3]}"
                if read_bad
                else ""
            )
        ),
        rows=read_rows,
    )
    return b, c, [s for s, _ in slugs]


@app.command
def worker(*, mode: str, index: int, start_at: float, payload: str) -> None:
    """Internal: one concurrent client. Not meant to be run by hand."""
    _worker(mode, index, start_at, json.loads(payload))


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
) -> None:
    """Run all three probes against a deployed target and report counts.

    Parameters
    ----------
    target: named target from the witan config (must be a remote one).
    racers: concurrent clients racing one task_claim (probe A).
    writers: concurrent clients writing distinct memories (probe B).
    readers: concurrent clients reading during those writes (probe C).
    lead: seconds each worker gets to connect before the synchronised fire.
    repo: repo key written on the probe's memories.
    keep: leave the probe's rows in the graph instead of cleaning up.
    """
    run_id = f"probe-{uuid.uuid4().hex[:8]}"
    os.environ["WITAN_TARGET"] = target
    # One token, fetched once and long enough to outlive the whole run, handed
    # to every worker -- see _srv() and _pinned_token().
    token, remaining = _pinned_token(target, needed_s=2 * lead + 180)
    # The PARENT keeps the normal refreshing provider: it is single-threaded, so
    # it cannot stampede, and its setup/verify/cleanup calls outlive the pinned
    # token's ~5min lifetime. Only the concurrent workers get the pinned one.
    srv = _srv(target)
    print(f"target={target} run={run_id} (token pinned, {remaining:.0f}s of life left)")
    print("-" * 72)

    a, scratch_slug = probe_mutual_exclusion(srv, target, racers, lead, run_id, token)
    b, c, mem_slugs = probe_writes_and_reads(
        srv, target, writers, readers, lead, run_id, scratch_slug, repo, token
    )

    for outcome in (a, b, c):
        print(
            f"[{'PASS' if outcome.passed else 'FAIL'}] {outcome.name}: {outcome.detail}"
        )
        for row in outcome.rows:
            if not row.get("ok") or not (row.get("result") or {}).get(
                "all_returned", True
            ):
                print(
                    f"      worker {row.get('index')}: "
                    f"{row.get('error_type')}: {row.get('error') or row.get('result')}"
                )

    if not keep:
        print("-" * 72)
        srv.task_close(
            slug=scratch_slug, resolution=f"concurrency probe {run_id} complete"
        )
        cleaned = 0
        for slug in mem_slugs:
            try:
                srv.memory_delete(slug=slug, confirm=True)
                cleaned += 1
            except BaseException as exc:  # noqa: BLE001
                print(f"  cleanup: {slug} left behind ({type(exc).__name__})")
        print(f"cleaned up scratch task + {cleaned}/{len(mem_slugs)} probe memories")

    raise SystemExit(0 if all(o.passed for o in (a, b, c)) else 1)


if __name__ == "__main__":
    app()
