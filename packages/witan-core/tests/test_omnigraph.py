"""Unit tests for the shared OmnigraphClient base.

Covers _find_binary lookup order, optimistic-concurrency conflict surfacing,
and the per-actor admission-cap backoff — the LOCAL/REMOTE-generic machinery.
Each server's own test_graph.py keeps only its subclass-specific bits (the
setup-hint message; witan-code's branch ops; witan's apply_schema).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from witan_core import omnigraph as og
from witan_core.omnigraph import OmnigraphClient, OmnigraphConflict


# ── store addressing: local --store vs remote --server/--graph ─────


def _built_client(monkeypatch, uri, **kwargs):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    return OmnigraphClient(uri, Path("/queries"), **kwargs)


def _force_cli(monkeypatch):
    """Pin a remote client to the CLI subprocess for tests that assert argv/env.

    A remote store now prefers the pooled HTTP transport for `query`/`mutate`
    (see omnigraph_http), so a test reaching the subprocess *through* `read()`
    would otherwise stop exercising the thing it names. The CLI path is not
    legacy — it is still the only way to `load`, `branch`, `optimize`, `cleanup`,
    `schema apply` or `repair` — so these contracts keep mattering, and the
    escape hatch is the supported way to select it.
    """
    monkeypatch.setenv(og.HTTP_TRANSPORT_ENV_VAR, "0")


def test_local_store_addressed_with_store_flag(monkeypatch):
    client = _built_client(monkeypatch, "/var/lib/witan/graph.omni", graph_id="council")
    assert client.is_remote is False
    # graph_id is carried but ignored for local addressing
    assert client._store_args() == ["--store", "/var/lib/witan/graph.omni"]


def test_s3_store_still_uses_store_flag(monkeypatch):
    client = _built_client(monkeypatch, "s3://bucket/graph", graph_id="council")
    assert client.is_remote is False
    assert client._store_args() == ["--store", "s3://bucket/graph"]


def test_remote_uses_server_and_graph_from_explicit_id(monkeypatch):
    client = _built_client(
        monkeypatch, "http://omnigraph-server:8080", graph_id="council"
    )
    assert client.is_remote is True
    assert client.server_url == "http://omnigraph-server:8080"
    assert client.graph_id == "council"
    assert client._store_args() == [
        "--server",
        "http://omnigraph-server:8080",
        "--graph",
        "council",
    ]


def test_remote_graph_id_parsed_from_uri_path(monkeypatch):
    client = _built_client(monkeypatch, "http://host:8080/graphs/code")
    assert client.server_url == "http://host:8080"
    assert client.graph_id == "code"


def test_store_cli_args_addresses_a_store_with_no_client():
    """The free-function form, for a tool driving a store it holds no client
    for — `witan migrate merge` addresses a source and a target through one
    client's binary, and hardcoding `--store` there is what made a deployed
    graph unreachable from the merge path."""
    assert og.store_cli_args("/var/lib/witan/graph.omni") == [
        "--store",
        "/var/lib/witan/graph.omni",
    ]
    assert og.store_cli_args("s3://bucket/graph") == ["--store", "s3://bucket/graph"]
    assert og.store_cli_args("http://host:8080", "council") == [
        "--server",
        "http://host:8080",
        "--graph",
        "council",
    ]
    # A remote store can be named completely in one argument, which is what
    # lets `--target` take a deployed graph with no second flag.
    assert og.store_cli_args("http://host:8080/graphs/council") == [
        "--server",
        "http://host:8080",
        "--graph",
        "council",
    ]


def test_store_subprocess_env_strips_an_ambient_token_for_a_local_store(monkeypatch):
    """A local path or s3:// root has no server to present a token to, so an
    ambient one is removed rather than merely not set — otherwise a token
    exported for cluster use rides into every local subprocess."""
    monkeypatch.setenv(og.BEARER_TOKEN_ENV_VAR, "ambient-secret")

    local = og.store_subprocess_env("/var/lib/witan/graph.omni")
    assert og.BEARER_TOKEN_ENV_VAR not in local

    s3 = og.store_subprocess_env("s3://bucket/graph", "explicit")
    assert og.BEARER_TOKEN_ENV_VAR not in s3

    # A remote store takes the explicit token when given...
    remote = og.store_subprocess_env("http://host:8080", "explicit")
    assert remote[og.BEARER_TOKEN_ENV_VAR] == "explicit"

    # ...and otherwise inherits the ambient one, the CLI's documented fallback
    # and the spelling an in-cluster maintenance pod relies on.
    inherited = og.store_subprocess_env("http://host:8080")
    assert inherited[og.BEARER_TOKEN_ENV_VAR] == "ambient-secret"


def test_explicit_graph_id_overrides_uri_path(monkeypatch):
    client = _built_client(
        monkeypatch, "http://host:8080/graphs/ignored", graph_id="council"
    )
    assert client.graph_id == "council"


def test_remote_without_graph_id_raises(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    with pytest.raises(ValueError, match="no graph id"):
        OmnigraphClient("http://host:8080", Path("/queries"))


def test_remote_without_host_raises(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    with pytest.raises(ValueError, match="no host"):
        OmnigraphClient("http://", Path("/queries"), graph_id="council")


def test_remote_rejects_underscore_graph_id(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    with pytest.raises(ValueError, match="invalid omnigraph graph id"):
        OmnigraphClient("http://host:8080", Path("/queries"), graph_id="code_repo")


def test_remote_run_builds_server_graph_command(monkeypatch):
    _force_cli(monkeypatch)
    client = _built_client(monkeypatch, "http://host:8080", graph_id="council")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    client.read("read.gq", "some_query", {})

    cmd = captured["cmd"]
    assert "--store" not in cmd
    assert cmd[cmd.index("--server") + 1] == "http://host:8080"
    assert cmd[cmd.index("--graph") + 1] == "council"


# ── bearer token delivery ─────────────────────────────────────────
#
# The CLI resolves a token from `OMNIGRAPH_TOKEN_<NAME>`, then
# ~/.omnigraph/credentials, then `OMNIGRAPH_BEARER_TOKEN` — and no subcommand
# takes a token flag. This client relies on that last fallback.
#
# The name was wrong for the whole life of the remote path (it read
# `OMNIGRAPH_SERVER_BEARER_TOKEN`, derived by analogy from the *server*-side
# `OMNIGRAPH_SERVER_BEARER_TOKENS_FILE`), so every remote call from both
# servers went out unauthenticated and the deployed migration Job crash-looped.
# Nothing caught it because nothing asserted WHICH variable was set.


def test_token_is_delivered_in_the_env_var_the_cli_actually_reads(monkeypatch):
    _force_cli(monkeypatch)
    client = _built_client(
        monkeypatch, "http://host:8080", graph_id="council", token="t"
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    client.read("read.gq", "some_query", {})

    # Pinned as a literal, not via the constant: asserting
    # env[og.BEARER_TOKEN_ENV_VAR] would pass for ANY value of the constant,
    # including the wrong one this test exists to prevent coming back.
    assert captured["env"]["OMNIGRAPH_BEARER_TOKEN"] == "t"
    assert "OMNIGRAPH_SERVER_BEARER_TOKEN" not in captured["env"]


def _token_seen_by_subprocess(monkeypatch, uri, *, token=None, ambient=None):
    """What the CLI subprocess would see in the token var for this address."""
    _force_cli(monkeypatch)
    if ambient is None:
        monkeypatch.delenv("OMNIGRAPH_BEARER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OMNIGRAPH_BEARER_TOKEN", ambient)
    client = _built_client(monkeypatch, uri, token=token, graph_id="council")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    client.read("read.gq", "some_query", {})
    return captured["env"].get("OMNIGRAPH_BEARER_TOKEN")


def test_no_token_invents_none(monkeypatch):
    assert _token_seen_by_subprocess(monkeypatch, "/var/lib/witan/graph.omni") is None


@pytest.mark.parametrize("uri", ["/var/lib/witan/graph.omni", "s3://bucket/graph"])
@pytest.mark.parametrize("token", [None, "explicit"])
def test_local_store_never_receives_a_bearer_token(monkeypatch, uri, token):
    """`env` is a copy of os.environ, so an ambient token exported for cluster
    use rode along into every local subprocess — a secret handed to a process
    that has no server to present it to. s3:// included: it authenticates with
    AWS credentials, not a bearer token."""
    assert (
        _token_seen_by_subprocess(
            monkeypatch, uri, token=token, ambient="ambient-secret"
        )
        is None
    )


def test_remote_store_keeps_an_ambient_token_when_none_is_configured(monkeypatch):
    """The complement of the rule above, and deliberately NOT stripped:
    `export OMNIGRAPH_BEARER_TOKEN=…` with nothing in config is the CLI's own
    documented fallback, so removing it here would break a supported way of
    driving a remote graph."""
    assert (
        _token_seen_by_subprocess(
            monkeypatch, "http://host:8080", ambient="ambient-secret"
        )
        == "ambient-secret"
    )


def test_explicit_token_overrides_an_ambient_one_on_a_remote_store(monkeypatch):
    assert (
        _token_seen_by_subprocess(
            monkeypatch, "http://host:8080", token="explicit", ambient="ambient-secret"
        )
        == "explicit"
    )


def test_each_call_gets_its_own_env_so_per_actor_tokens_cannot_race(monkeypatch):
    """witan resolves a different token per request (ADR-0004). The env is
    built per `_execute`, so two clients' tokens never share mutable state —
    which is why this uses the env fallback and not `omnigraph login`, whose
    credentials file is process-global."""
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs["env"].get("OMNIGRAPH_BEARER_TOKEN"))
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    _force_cli(monkeypatch)
    monkeypatch.setattr(og.subprocess, "run", fake_run)
    for token in ("actor-a-token", "actor-b-token"):
        _built_client(
            monkeypatch, "http://host:8080", graph_id="council", token=token
        ).read("read.gq", "q", {})

    assert seen == ["actor-a-token", "actor-b-token"]


@pytest.mark.skipif(
    not os.environ.get("WITAN_TEST_OMNIGRAPH_SERVER"),
    reason="set WITAN_TEST_OMNIGRAPH_SERVER=<url> to check token delivery live",
)
def test_live_server_reads_the_token_we_send(monkeypatch):
    """The only check that catches omnigraph RENAMING the variable on us.

    Every other test here pins our side of the contract; none would notice if
    a future omnigraph stopped reading `OMNIGRAPH_BEARER_TOKEN`. The CLI's own
    wording is the discriminator: "missing bearer token" means it found no
    token at all (wrong variable), "invalid bearer token" means it read ours
    and rejected the value — which is the expected outcome for a bogus token
    and proves delivery works.

    Opt-in because it needs a reachable server; point it at a port-forward:
        kubectl -n omnigraph port-forward svc/omnigraph-server 18080:8080
        WITAN_TEST_OMNIGRAPH_SERVER=http://127.0.0.1:18080 pytest -k live_server

    ``WITAN_TEST_OMNIGRAPH_GRAPH`` names a graph the server serves (default
    ``council``). Any remote command works — auth is checked before the graph
    is touched — but the client requires a graph id to build a remote address
    at all, so a server-level command is not actually simpler here.
    """
    url = os.environ["WITAN_TEST_OMNIGRAPH_SERVER"]
    graph = os.environ.get("WITAN_TEST_OMNIGRAPH_GRAPH", "council")
    client = OmnigraphClient(
        url, Path("/queries"), token="deliberately-bogus", graph_id=graph
    )
    with pytest.raises(RuntimeError) as exc:
        client._run("snapshot")

    message = str(exc.value).lower()
    assert "invalid bearer token" in message, (
        "expected the server to reject our bogus token; got "
        f"{message!r}. 'missing bearer token' here means the CLI no longer "
        f"reads {og.BEARER_TOKEN_ENV_VAR} — re-check its token-resolution order."
    )


def test_find_binary_prefers_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    assert OmnigraphClient._find_binary() == "/usr/bin/omnigraph"


def test_find_binary_falls_back_to_local_bin_when_not_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    fallback = tmp_path / ".local" / "bin" / "omnigraph"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("")

    assert OmnigraphClient._find_binary() == str(fallback)


def test_find_binary_raises_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="omnigraph binary not found"):
        OmnigraphClient._find_binary()


def test_is_storage_version_mismatch_detects_marker_pair():
    msg = "graph is stamped at internal schema 5 but this binary reads only 4"
    assert og._is_storage_version_mismatch(msg) is True
    assert og._is_storage_version_mismatch("some other omnigraph error") is False


# ── conflict surfacing (CAS support) ──────────────────────────────


def _client(monkeypatch):
    """A base client over a remote server (skips the local write lock), with the
    binary lookup stubbed so no real omnigraph is needed."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    return OmnigraphClient("https://graph.example", Path("/queries"), graph_id="g")


def _stub_run(monkeypatch, *, returncode, stderr):
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)
    return calls


def test_surface_conflict_raises_on_occ_conflict(monkeypatch):
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr="commit failed: stale view")

    with pytest.raises(OmnigraphConflict):
        client._execute(
            ["omnigraph", "mutate"], "mutate", is_write=True, surface_conflict=True
        )
    assert calls["n"] == 1  # surfaced immediately, no clobbering retry


def test_conflict_is_retried_when_not_surfaced(monkeypatch):
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr="commit failed: stale view")

    with pytest.raises(RuntimeError, match="failed"):
        client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert calls["n"] > 1


# ── per-actor admission cap backoff ───────────────────────────────


def test_admission_cap_backoff_adds_bounded_jitter():
    delay = og._ADMISSION_CAP_BASE_DELAY * (2 ** (3 - 1))  # attempt=3
    for _ in range(50):
        backoff = og._admission_cap_backoff(3)
        assert delay <= backoff <= delay * 1.1
    assert og._admission_cap_backoff(20) == og._ADMISSION_CAP_MAX_DELAY


def test_inflight_cap_retries_then_succeeds(monkeypatch):
    client = _client(monkeypatch)
    calls = {"n": 0}
    sleeps = []

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="actor in-flight count cap 16 exceeded"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    monkeypatch.setattr(og.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(og.random, "uniform", lambda a, b: 0.0)  # no jitter

    out = client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert out == "ok"
    assert calls["n"] == 3
    assert sleeps == [0.25, 0.5]  # base 0.25s, doubling — independent oracle


def test_byte_budget_exceeded_also_retries(monkeypatch):
    client = _client(monkeypatch)
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="actor byte budget exceeded: would use 999 bytes against cap 100",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    monkeypatch.setattr(og.time, "sleep", lambda *_: None)

    out = client._execute(["omnigraph", "load"], "load", is_write=True)
    assert out == "ok"
    assert calls["n"] == 2


def test_inflight_cap_gives_up_after_its_own_budget(monkeypatch):
    client = _client(monkeypatch)
    calls = _stub_run(
        monkeypatch, returncode=1, stderr="actor in-flight count cap 16 exceeded"
    )

    with pytest.raises(RuntimeError, match="admission cap exceeded"):
        client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert calls["n"] == og._ADMISSION_CAP_MAX_ATTEMPTS


def test_inflight_cap_ignores_surface_conflict(monkeypatch):
    client = _client(monkeypatch)
    _stub_run(monkeypatch, returncode=1, stderr="actor in-flight count cap 16 exceeded")

    with pytest.raises(RuntimeError, match="admission cap exceeded"):
        client._execute(
            ["omnigraph", "mutate"], "mutate", is_write=True, surface_conflict=True
        )


# ── server-unavailable (restart window) backoff ───────────────────

# Verbatim stderr from `omnigraph query --server http://127.0.0.1:<dead-port>`
# (omnigraph 0.8.1), ANSI codes and all — the markers have to survive the
# colour spans the CLI wraps each cause line in.
_CONNECT_REFUSED_STDERR = (
    "Error: \n"
    "   0: \x1b[91merror sending request for url "
    "(http://127.0.0.1:59999/graphs/council/read)\x1b[0m\n"
    "   1: \x1b[91mclient error (Connect)\x1b[0m\n"
    "   2: \x1b[91mtcp connect error\x1b[0m\n"
    "   3: \x1b[91mConnection refused (os error 111)\x1b[0m\n"
)


# Longest outage measured against the real CI deployment (2026-08-03): two
# separate restarts of the omnigraph-server Deployment, one triggered by adding
# a token to the map and one by removing it, from the moment the old container
# was killed to the moment the new pod reported Ready.
#
#     killed 21:20:21Z -> Ready 21:21:22Z   61s
#     killed 21:33:36Z -> Ready 21:34:28Z   52s
_MEASURED_RESTART_OUTAGE_SECONDS = 61


def _fake_clock(monkeypatch, *, jitter=False):
    """Stub sleep so it advances a fake monotonic clock instead of blocking.

    The budget is a wall-clock deadline, so a no-op sleep would spin forever —
    the clock has to move for the loop to terminate. Returns the sleep log.
    """
    sleeps = []
    now = [1000.0]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(og.time, "sleep", fake_sleep)
    monkeypatch.setattr(og.time, "monotonic", lambda: now[0])
    if not jitter:
        monkeypatch.setattr(og.random, "uniform", lambda a, b: 0.0)
    return sleeps


def test_unavailable_backoff_adds_bounded_jitter():
    delay = og._UNAVAILABLE_BASE_DELAY * (2 ** (3 - 1))  # attempt=3
    for _ in range(50):
        backoff = og._unavailable_backoff(3)
        assert delay <= backoff <= delay * 1.1
    assert og._unavailable_backoff(20) == og._UNAVAILABLE_MAX_DELAY


def test_connect_failure_retries_until_server_returns(monkeypatch):
    """The restart window: the server is simply absent, then comes back."""
    client = _client(monkeypatch)
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] < 4:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr=_CONNECT_REFUSED_STDERR
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    sleeps = _fake_clock(monkeypatch)

    out = client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert out == "ok"
    assert calls["n"] == 4
    assert sleeps == [0.5, 1.0, 2.0]  # base 0.5s, doubling — independent oracle


def test_budget_outlasts_a_real_measured_restart(monkeypatch):
    """The regression guard that the original attempt-count budget failed.

    The first version of this budget was 12 attempts of capped backoff — ~42s —
    and its test asserted that sum was ">= 40", a number with no provenance.
    It passed while being wrong: the real outage is 52-61s, so the client gave
    up mid-restart. Assert against the MEASURED outage instead of the
    schedule's own arithmetic, and drive it through _execute so the deadline
    logic is what's under test rather than a formula reimplemented here.
    """
    client = _client(monkeypatch)
    outage_end = 1000.0 + _MEASURED_RESTART_OUTAGE_SECONDS
    calls = {"n": 0}
    now = [1000.0]

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if now[0] < outage_end:  # server still down
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr=_CONNECT_REFUSED_STDERR
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="back", stderr="")

    def fake_sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    monkeypatch.setattr(og.time, "sleep", fake_sleep)
    monkeypatch.setattr(og.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(og.random, "uniform", lambda a, b: 0.0)

    assert client._execute(["omnigraph", "query"], "query", is_write=False) == "back"
    # And with real headroom left over, not scraping the deadline.
    assert now[0] - 1000.0 < og._UNAVAILABLE_MAX_WAIT * 0.75


def test_connect_failure_gives_up_after_its_own_budget(monkeypatch):
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr=_CONNECT_REFUSED_STDERR)
    sleeps = _fake_clock(monkeypatch)

    with pytest.raises(
        RuntimeError, match="could not connect to https://graph.example"
    ):
        client._execute(["omnigraph", "query"], "query", is_write=False)
    # Bounded by wall clock, not a fixed attempt count — and it spends the
    # WHOLE budget. An earlier version skipped the last sleep whenever the
    # next backoff would overshoot the deadline, which quietly cut the
    # effective window to 145.5s of the configured 150s.
    assert sum(sleeps) == pytest.approx(og._UNAVAILABLE_MAX_WAIT)
    assert calls["n"] > _MEASURED_RESTART_OUTAGE_SECONDS / og._UNAVAILABLE_MAX_DELAY


def test_final_retry_lands_on_the_deadline_not_before_it(monkeypatch):
    """The budget must be spent to the last second, including one attempt at
    the deadline itself — a server that comes back at T-1s inside the window
    has to be caught, not missed because the next backoff would overshoot."""
    client = _client(monkeypatch)
    # Comes back with 1s of budget left: inside the window by any reading, but
    # far past the last un-clamped backoff boundary (145.5s).
    back_at = 1000.0 + og._UNAVAILABLE_MAX_WAIT - 1
    now = [1000.0]

    def fake_run(cmd, **kwargs):
        if now[0] < back_at:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr=_CONNECT_REFUSED_STDERR
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="back", stderr="")

    def fake_sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    monkeypatch.setattr(og.time, "sleep", fake_sleep)
    monkeypatch.setattr(og.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(og.random, "uniform", lambda a, b: 0.0)

    assert client._execute(["omnigraph", "query"], "query", is_write=False) == "back"


def test_connect_failure_ignores_surface_conflict(monkeypatch):
    """There is no conflict to surface — the request never left this process."""
    client = _client(monkeypatch)
    _stub_run(monkeypatch, returncode=1, stderr=_CONNECT_REFUSED_STDERR)
    _fake_clock(monkeypatch)

    with pytest.raises(RuntimeError, match="could not connect"):
        client._execute(
            ["omnigraph", "mutate"], "mutate", is_write=True, surface_conflict=True
        )


def test_midflight_failure_is_not_retried(monkeypatch):
    """A reset/timeout after the request was sent may have committed the write.
    Deliberately excluded from _UNAVAILABLE_MARKERS — fail loudly instead."""
    client = _client(monkeypatch)
    calls = _stub_run(
        monkeypatch,
        returncode=1,
        stderr="error sending request for url (...): connection reset by peer",
    )

    with pytest.raises(RuntimeError, match="failed"):
        client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert calls["n"] == 1


def test_connect_retry_off_fails_immediately(monkeypatch):
    """A caller whose answer DEGRADES — an existence check, a listing — must
    not spend the restart budget to report "no". 150s to say a graph might not
    be there is worse than saying it now."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    client = OmnigraphClient(
        "https://graph.example", Path("/queries"), graph_id="g", connect_retry=False
    )
    calls = _stub_run(monkeypatch, returncode=1, stderr=_CONNECT_REFUSED_STDERR)

    with pytest.raises(RuntimeError, match="failed"):
        client._execute(["omnigraph", "branch"], "branch", is_write=False)
    assert calls["n"] == 1


def test_local_store_does_not_take_the_unavailable_path(monkeypatch):
    """`tcp connect error` from a local store is not a restarting server, so it
    must not be retried on the remote budget."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    client = OmnigraphClient("/var/lib/witan/graph.omni", Path("/queries"))
    calls = _stub_run(monkeypatch, returncode=1, stderr=_CONNECT_REFUSED_STDERR)

    with pytest.raises(RuntimeError, match="failed"):
        client._execute(["omnigraph", "query"], "query", is_write=False)
    assert calls["n"] == 1


# ── re-entrant store write lock ────────────────────────────────────


def test_a_write_inside_hold_write_lock_does_not_self_deadlock(monkeypatch, tmp_path):
    """The trap: flock belongs to the open file description, not the process.

    `merge_store` holds the target's lock across export → reconcile → load so
    another writer cannot make the decisions stale. The load inside is an
    ordinary client write, which takes the same lock — and a second `open()` of
    one lock file blocks even the thread already holding it, so the obvious
    nesting is a self-deadlock, not a no-op. Without the re-entrancy this test
    hangs rather than fails.
    """
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    store = tmp_path / "graph.omni"
    client = OmnigraphClient(str(store), Path("/queries"))
    _recording_run(monkeypatch)

    with client.hold_write_lock():
        client.load_batch([{"type": "Memory", "data": {"slug": "m"}}])

    # And the lock is fully released afterwards, not left pinned at depth 1 —
    # a leaked depth would silently disable locking for the rest of the process.
    assert not og._held_flocks
    fh = og.acquire_store_flock(str(store))
    assert fh is not None
    og.release_store_flock(str(store), fh)


def test_nested_acquisitions_unlock_only_at_depth_zero(tmp_path):
    store = str(tmp_path / "graph.omni")
    outer = og.acquire_store_flock(store)
    inner = og.acquire_store_flock(store)

    assert outer is not None
    assert inner is None  # re-entry, nothing of its own to release

    og.release_store_flock(store, inner)
    assert og._held_flocks  # still held by the outer acquisition
    og.release_store_flock(store, outer)
    assert not og._held_flocks


def test_a_read_inside_hold_write_lock_is_unaffected(monkeypatch, tmp_path):
    """Reads take no write lock, so they must neither block nor decrement the
    depth the surrounding write lock is counting on."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    store = tmp_path / "graph.omni"
    client = OmnigraphClient(str(store), Path("/queries"))
    _recording_run(monkeypatch)

    with client.hold_write_lock():
        client._execute(["omnigraph", "query"], "query", is_write=False)
        assert og._held_flocks  # the outer lock survived the read
    assert not og._held_flocks


# ── export_to: streaming, under the same retry policy ──────────────


def _export_stub(monkeypatch, outcomes):
    """Stub subprocess.run for `export`, consuming one `outcomes` entry per call.

    Each entry is ``(returncode, stdout_text)``; the text is written to the
    file the caller redirected stdout to, so a test can assert on what a retry
    left behind rather than only on the return code.
    """
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        rc, text = outcomes[min(calls["n"], len(outcomes) - 1)]
        calls["n"] += 1
        if text:
            kwargs["stdout"].write(text)
        stderr = "" if rc == 0 else _CONNECT_REFUSED_STDERR
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=stderr)

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    return calls


def test_export_retries_a_connect_refusal_until_the_server_returns(
    monkeypatch, tmp_path
):
    """The regression: a merge's target export died on the first connect refusal.

    omnigraph-server is `replicas=1` + `strategy=Recreate`, so every restart is
    a hard endpoint gap — and one landed 43s before a real cutover attempt,
    killing the merge with a raw Rust backtrace. Every other omnigraph call
    already rode this out; `export` did not, because `store_merge` shelled out
    around the client instead of through it.
    """
    client = _client(monkeypatch)
    _fake_clock(monkeypatch)
    out = tmp_path / "target.jsonl"
    calls = _export_stub(monkeypatch, [(1, ""), (1, ""), (0, '{"type":"Memory"}\n')])

    client.export_to(out, label="export (target)")

    assert calls["n"] == 3  # two refusals ridden out, third succeeded
    assert out.read_text() == '{"type":"Memory"}\n'


def test_export_truncates_between_attempts(monkeypatch, tmp_path):
    """A retry must overwrite the failed attempt's partial output, not append to it.

    An export streams straight to the file, so a server that dies mid-export
    leaves a prefix on disk. Appending the successful retry onto it would
    produce a file with duplicated rows — which `_parse_export` would happily
    read, silently doubling the merge's target index.
    """
    client = _client(monkeypatch)
    _fake_clock(monkeypatch)
    out = tmp_path / "target.jsonl"
    calls = _export_stub(
        monkeypatch,
        [(1, '{"type":"Memory","data":{"slug":"half'), (0, '{"type":"Memory"}\n')],
    )

    client.export_to(out)

    assert calls["n"] == 2
    assert out.read_text() == '{"type":"Memory"}\n'  # no truncated prefix left


def test_export_gives_up_as_store_unavailable(monkeypatch, tmp_path):
    """Exhausting the budget raises the typed error, so a caller can say
    something useful — `store_merge` turns it into "temporarily unavailable,
    safe to re-run" instead of relaying a ClusterIP the user cannot reach."""
    client = _client(monkeypatch)
    _fake_clock(monkeypatch)
    calls = _export_stub(monkeypatch, [(1, "")])

    with pytest.raises(og.StoreUnavailable, match="could not connect to"):
        client.export_to(tmp_path / "target.jsonl", label="export (deployed graph)")
    assert calls["n"] > 1
    # Still a RuntimeError, so existing `except RuntimeError` handlers are
    # unaffected by the new type.
    assert issubclass(og.StoreUnavailable, RuntimeError)


def test_export_streams_rather_than_buffering(monkeypatch, tmp_path):
    """Output must be redirected to the file, never captured into a string —
    an export is the whole graph, and capturing it would hold it in memory on
    top of the parsed rows."""
    client = _client(monkeypatch)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        seen["cmd"] = cmd
        kwargs["stdout"].write("{}\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    client.export_to(tmp_path / "out.jsonl")

    assert seen.get("capture_output") is not True
    assert hasattr(seen["stdout"], "write")  # a file handle, not PIPE
    assert seen["cmd"][:2] == ["/usr/bin/omnigraph", "export"]
    assert seen["cmd"][2:] == client._store_args()


# ── schema apply + mtime stamp ─────────────────────────────────────


@pytest.fixture
def fake_schema(tmp_path):
    schema = tmp_path / "schema.pg"
    schema.write_text("node Memory { slug: String }")
    return schema


def _recording_run(monkeypatch, returncode=0):
    """Replace subprocess.run in the omnigraph module, recording each call."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, "", "")

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    return calls


def test_schema_apply_stamps_the_schema_mtime(monkeypatch, tmp_path, fake_schema):
    store = tmp_path / "graph.omni"
    store.mkdir()
    _recording_run(monkeypatch)

    assert og.schema_apply("omnigraph", fake_schema, store) is True
    assert og.schema_stamp_path(store).read_text() == str(fake_schema.stat().st_mtime)


def test_schema_apply_does_not_stamp_on_failure(monkeypatch, tmp_path, fake_schema):
    """A failed apply must be retried next time, not recorded as done."""
    store = tmp_path / "graph.omni"
    store.mkdir()
    _recording_run(monkeypatch, returncode=1)

    assert og.schema_apply("omnigraph", fake_schema, store) is False
    assert not og.schema_stamp_path(store).exists()


def test_schema_apply_never_raises_on_a_failed_apply(
    monkeypatch, tmp_path, fake_schema
):
    """witan's _ensure_graph calls this at import time — a raise here would
    take down `witan serve` at startup rather than degrading."""
    store = tmp_path / "graph.omni"
    store.mkdir()

    def boom(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "schema apply exploded")

    monkeypatch.setattr(og.subprocess, "run", boom)
    assert og.schema_apply("omnigraph", fake_schema, store) is False


def test_apply_if_changed_skips_when_the_stamp_matches(
    monkeypatch, tmp_path, fake_schema
):
    store = tmp_path / "graph.omni"
    store.mkdir()
    calls = _recording_run(monkeypatch)

    og.schema_apply_if_changed("omnigraph", fake_schema, store)
    assert len(calls) == 1  # first call applies
    og.schema_apply_if_changed("omnigraph", fake_schema, store)
    og.schema_apply_if_changed("omnigraph", fake_schema, store)
    assert len(calls) == 1  # subsequent calls are subprocess-free


def test_apply_if_changed_reapplies_when_the_schema_changes(
    monkeypatch, tmp_path, fake_schema
):
    """The whole point: an additive schema change reaches an existing store."""
    store = tmp_path / "graph.omni"
    store.mkdir()
    calls = _recording_run(monkeypatch)

    og.schema_apply_if_changed("omnigraph", fake_schema, store)
    assert len(calls) == 1

    fake_schema.write_text("node Memory { slug: String }\nnode Topic { slug: String }")
    os.utime(fake_schema, (2_000_000_000, 2_000_000_000))

    og.schema_apply_if_changed("omnigraph", fake_schema, store)
    assert len(calls) == 2
    assert calls[1][:3] == ["omnigraph", "schema", "apply"]


def test_apply_if_changed_retries_after_a_failed_apply(
    monkeypatch, tmp_path, fake_schema
):
    store = tmp_path / "graph.omni"
    store.mkdir()
    calls = _recording_run(monkeypatch, returncode=1)

    og.schema_apply_if_changed("omnigraph", fake_schema, store)
    og.schema_apply_if_changed("omnigraph", fake_schema, store)
    assert len(calls) == 2


def test_schema_apply_returns_false_when_the_binary_is_missing(tmp_path, fake_schema):
    """subprocess.run raises OSError for a missing/non-executable binary, which
    would escape the same import-time path that dropping check=True protects."""
    store = tmp_path / "graph.omni"
    store.mkdir()

    assert og.schema_apply("/nonexistent/omnigraph", fake_schema, store) is False
    assert not og.schema_stamp_path(store).exists()


def test_apply_if_changed_returns_false_when_the_schema_is_unstattable(
    monkeypatch, tmp_path
):
    """No schema file means nothing to apply — don't spawn a subprocess for it."""
    store = tmp_path / "graph.omni"
    store.mkdir()
    calls = _recording_run(monkeypatch)

    assert og.schema_apply_if_changed("omnigraph", tmp_path / "gone.pg", store) is False
    assert calls == []


def test_apply_if_changed_reapplies_when_the_stamp_is_unreadable(
    monkeypatch, tmp_path, fake_schema
):
    """An unreadable stamp means the last-applied mtime is unknown, i.e. the
    store *might* be stale. Fall through to a redundant idempotent apply rather
    than assuming current — skipping one silently leaves the store behind."""
    store = tmp_path / "graph.omni"
    store.mkdir()
    calls = _recording_run(monkeypatch)

    og.schema_apply_if_changed("omnigraph", fake_schema, store)
    assert len(calls) == 1

    stamp = og.schema_stamp_path(store)

    def boom(*_a, **_kw):
        raise OSError("stamp is unreadable")

    monkeypatch.setattr(type(stamp), "read_text", boom)

    og.schema_apply_if_changed("omnigraph", fake_schema, store)
    assert len(calls) == 2
