"""Unit tests for the shared OmnigraphClient base.

Covers _find_binary lookup order, optimistic-concurrency conflict surfacing,
and the per-actor admission-cap backoff — the LOCAL/REMOTE-generic machinery.
Each server's own test_graph.py keeps only its subclass-specific bits (the
setup-hint message; witan-code's branch ops; witan's apply_schema).
"""

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from witan_core import omnigraph as og
from witan_core import omnigraph_http as _http
from witan_core.omnigraph import OmnigraphClient, OmnigraphConflict


# ── store addressing: local --store vs remote --server/--graph ─────


# `some_query`/`q` cover every no-params `.read(...)` call below — the CLI
# tests here exercise argv/env construction, not query content, so neither
# declares a parameter.
_READ_GQ = """
query some_query() {
    match (m: Memory) return m.slug
}

query q() {
    match (m: Memory) return m.slug
}
"""


def _built_client(monkeypatch, uri, *, queries_dir=None, **kwargs):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    return OmnigraphClient(uri, queries_dir or Path("/queries"), **kwargs)


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


def test_remote_run_builds_server_graph_command(monkeypatch, tmp_path):
    _force_cli(monkeypatch)
    (tmp_path / "read.gq").write_text(_READ_GQ)
    client = _built_client(
        monkeypatch, "http://host:8080", queries_dir=tmp_path, graph_id="council"
    )
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


def test_token_is_delivered_in_the_env_var_the_cli_actually_reads(
    monkeypatch, tmp_path
):
    _force_cli(monkeypatch)
    (tmp_path / "read.gq").write_text(_READ_GQ)
    client = _built_client(
        monkeypatch,
        "http://host:8080",
        queries_dir=tmp_path,
        graph_id="council",
        token="t",
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


def _token_seen_by_subprocess(monkeypatch, uri, tmp_path, *, token=None, ambient=None):
    """What the CLI subprocess would see in the token var for this address."""
    _force_cli(monkeypatch)
    if ambient is None:
        monkeypatch.delenv("OMNIGRAPH_BEARER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OMNIGRAPH_BEARER_TOKEN", ambient)
    (tmp_path / "read.gq").write_text(_READ_GQ)
    client = _built_client(
        monkeypatch, uri, queries_dir=tmp_path, token=token, graph_id="council"
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    client.read("read.gq", "some_query", {})
    return captured["env"].get("OMNIGRAPH_BEARER_TOKEN")


def test_no_token_invents_none(monkeypatch, tmp_path):
    assert (
        _token_seen_by_subprocess(monkeypatch, "/var/lib/witan/graph.omni", tmp_path)
        is None
    )


@pytest.mark.parametrize("uri", ["/var/lib/witan/graph.omni", "s3://bucket/graph"])
@pytest.mark.parametrize("token", [None, "explicit"])
def test_local_store_never_receives_a_bearer_token(monkeypatch, tmp_path, uri, token):
    """`env` is a copy of os.environ, so an ambient token exported for cluster
    use rode along into every local subprocess — a secret handed to a process
    that has no server to present it to. s3:// included: it authenticates with
    AWS credentials, not a bearer token."""
    assert (
        _token_seen_by_subprocess(
            monkeypatch, uri, tmp_path, token=token, ambient="ambient-secret"
        )
        is None
    )


def test_remote_store_keeps_an_ambient_token_when_none_is_configured(
    monkeypatch, tmp_path
):
    """The complement of the rule above, and deliberately NOT stripped:
    `export OMNIGRAPH_BEARER_TOKEN=…` with nothing in config is the CLI's own
    documented fallback, so removing it here would break a supported way of
    driving a remote graph."""
    assert (
        _token_seen_by_subprocess(
            monkeypatch, "http://host:8080", tmp_path, ambient="ambient-secret"
        )
        == "ambient-secret"
    )


def test_explicit_token_overrides_an_ambient_one_on_a_remote_store(
    monkeypatch, tmp_path
):
    assert (
        _token_seen_by_subprocess(
            monkeypatch,
            "http://host:8080",
            tmp_path,
            token="explicit",
            ambient="ambient-secret",
        )
        == "explicit"
    )


def test_each_call_gets_its_own_env_so_per_actor_tokens_cannot_race(
    monkeypatch, tmp_path
):
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
    (tmp_path / "read.gq").write_text(_READ_GQ)
    for token in ("actor-a-token", "actor-b-token"):
        _built_client(
            monkeypatch,
            "http://host:8080",
            queries_dir=tmp_path,
            graph_id="council",
            token=token,
        ).read("read.gq", "q", {})

    assert seen == ["actor-a-token", "actor-b-token"]


# ── read path: declared-vs-supplied params ──────────────────────────


_PARAMETERIZED_READ_GQ = """
query by_slug($slug: String, $tags: [String]?) {
    match (m: Memory) where m.slug = $slug return m.slug
}
"""


def test_read_raises_on_a_missing_declared_parameter(monkeypatch, tmp_path):
    """Mirrors the write side's check (test_compose_batch_names_a_missing_
    parameter in test_batching.py): a read that omits a declared parameter
    must fail here, naming it, rather than reach the CLI/server with an
    unbound query variable — which on a pre-#569 omnigraph silently widened
    the read to every row instead of erroring."""
    _force_cli(monkeypatch)
    (tmp_path / "read.gq").write_text(_PARAMETERIZED_READ_GQ)
    client = _built_client(
        monkeypatch, "http://host:8080", queries_dir=tmp_path, graph_id="council"
    )
    calls = []
    monkeypatch.setattr(og.subprocess, "run", lambda cmd, **kw: calls.append(cmd))

    with pytest.raises(KeyError, match="slug"):
        client.read("read.gq", "by_slug", {"tags": None})

    assert calls == []  # never reached the subprocess


def test_read_accepts_an_explicit_none_for_an_optional_parameter(monkeypatch, tmp_path):
    """`tags` is optional and supplied explicitly as `None` — the same
    convention `change` already uses — so it must count as supplied, not
    missing."""
    _force_cli(monkeypatch)
    (tmp_path / "read.gq").write_text(_PARAMETERIZED_READ_GQ)
    client = _built_client(
        monkeypatch, "http://host:8080", queries_dir=tmp_path, graph_id="council"
    )
    monkeypatch.setattr(
        og.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr=""),
    )

    assert client.read("read.gq", "by_slug", {"slug": "x", "tags": None}) == []


def test_read_declared_params_cache_picks_up_an_edited_query(monkeypatch, tmp_path):
    """Same mtime-keyed cache shape as `_cached_query_text` — a query file
    edited to require a new parameter must not be served from a stale
    declaration set."""
    _force_cli(monkeypatch)
    query_path = tmp_path / "read.gq"
    query_path.write_text("query q($a: String) { match (m: Memory) return m.slug }")
    client = _built_client(
        monkeypatch, "http://host:8080", queries_dir=tmp_path, graph_id="council"
    )
    monkeypatch.setattr(
        og.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr=""),
    )
    assert client.read("read.gq", "q", {"a": "x"}) == []

    later = query_path.stat().st_mtime + 2
    query_path.write_text(
        "query q($a: String, $b: String) { match (m: Memory) return m.slug }"
    )
    os.utime(query_path, (later, later))

    with pytest.raises(KeyError, match="b"):
        client.read("read.gq", "q", {"a": "x"})


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


# The remote write-authority precondition, verbatim from a QA `task_claim` race
# on 2026-08-17 (8 racers, witan 0.16.0). Kept as a literal because every defect
# here has been a wording the classifier did not anticipate — a paraphrase would
# test the paraphrase.
_WRITE_AUTHORITY_STDERR = (
    "omnigraph mutate failed:\n"
    "write authority 'graph_head:main' changed during preparation "
    "(expected 01M08E24Y2WWC3QVE9MD3K6CWN, current 01M08E27K5J56HF8QZ7GX61X3F) "
    "— reprepare from the current branch state (HTTP 409, conflict)"
)


def test_write_authority_conflict_is_retryable_not_fatal():
    """The CLI path has no status, so this wording is the only signal there is.

    Classified FATAL, it took down the compare-and-swap path entirely: 6 of 8
    concurrent `task_claim` racers got an opaque RuntimeError where the contract
    promises a structured refusal.
    """
    assert og._classify_cli_error(_WRITE_AUTHORITY_STDERR) == _http.RETRYABLE


# ── omnigraph's 2026-08-20 vocabulary rename (upstream 69d292ce80) ──────────
#
# It reworded the error text `_classify_cli_error` matches on, WITHOUT bumping
# the reported version — so both spellings are in the wild simultaneously and
# both have to classify identically. Literals, not paraphrases, for the reason
# `_WRITE_AUTHORITY_STDERR` is: every defect here has been a wording the
# classifier did not anticipate. Taken from the two builds' own strings.

_STALE_VIEW_OLD = (
    "omnigraph mutate failed:\n"
    "stale view of 'node:Doc': expected manifest table version 6 "
    "but current is 7 — refresh and retry"
)
_STALE_VIEW_NEW = (
    "omnigraph mutate failed:\n"
    "stale view of dataset for node type 'Doc': expected published dataset "
    "version 6 but current is 7 — refresh and retry"
)
_DRIFT_OLD = (
    "omnigraph mutate failed:\n"
    "table 'node:Doc' has Lance HEAD version 9 ahead of manifest version 8; "
    "run `omnigraph repair` before writing"
)
_DRIFT_NEW = (
    "omnigraph mutate failed:\n"
    "dataset for node type 'Doc' is at Lance HEAD version 9, ahead of "
    "published dataset version 8; run `omnigraph repair` before writing"
)
_RECLAIMED_OLD = "omnigraph query failed:\nhistorical table version 7 was reclaimed"
_RECLAIMED_NEW = (
    "omnigraph query failed:\nhistorical published dataset version 7 was reclaimed"
)


@pytest.mark.parametrize("stderr", [_STALE_VIEW_OLD, _STALE_VIEW_NEW])
def test_stale_view_is_retryable_in_both_vocabularies(stderr):
    assert og._classify_cli_error(stderr) == _http.RETRYABLE


@pytest.mark.parametrize("stderr", [_DRIFT_OLD, _DRIFT_NEW])
def test_head_drift_needs_repair_in_both_vocabularies(stderr):
    """NEEDS_REPAIR, not RETRYABLE — and the new wording is the one that can go
    wrong, since it contains "published dataset version" too. `_NEEDS_REPAIR`
    is checked first in `_classify_cli_error`; this pins that order."""
    assert og._classify_cli_error(stderr) == _http.NEEDS_REPAIR


@pytest.mark.parametrize("stderr", [_RECLAIMED_OLD, _RECLAIMED_NEW])
def test_a_reclaimed_historical_version_stays_fatal_in_both_vocabularies(stderr):
    """The reason `_RETRYABLE` matches "expected published dataset version" and
    not the bare phrase. A reclaimed version is gone: retrying re-reads the same
    absence until the attempt budget runs out, then reports a timeout-shaped
    failure for what is actually a permanent one."""
    assert og._classify_cli_error(stderr) == _http.FATAL


def test_surface_conflict_loses_the_race_on_a_write_authority_conflict(monkeypatch):
    """The whole point of the fix: a CAS caller must get `OmnigraphConflict`.

    That is what `task_claim` catches to re-read and answer `lost_race`. And it
    must surface on the FIRST attempt — a retry would re-apply the claim over
    whoever actually won.
    """
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr=_WRITE_AUTHORITY_STDERR)

    with pytest.raises(OmnigraphConflict, match="write authority"):
        client._execute(
            ["omnigraph", "mutate"], "mutate", is_write=True, surface_conflict=True
        )
    assert calls["n"] == 1


# The CLI's own wording for the two conditions `classify_status` keys on by
# status. `omnigraph` prints the message and discards the response, so on this
# path the prose is the ONLY signal — which is exactly why these need their own
# tests rather than being assumed covered by the HTTP ones.
_PRECONDITION_STDERR = (
    "omnigraph mutate failed:\n"
    "precondition_failure {expected: 01M08E24Y2WWC3QVE9MD3K6CWN, "
    "actual: 01M08E27K5J56HF8QZ7GX61X3F}"
)
_RECOVERY_STDERR = (
    "omnigraph mutate failed:\n"
    "recovery required for operation 01KZY7Q0J2: pending Load recovery "
    "operation blocks writes on branch 'main'"
)


def test_cli_precondition_failure_is_terminal(monkeypatch):
    """★ THE CLI PATH HAS NO STATUS TO READ, so this is the only thing standing
    between a 412-equivalent and the retry loop.

    One attempt, then a hard error. A retry here re-sends a write the server has
    told us it will never replay, over whoever won the race.
    """
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr=_PRECONDITION_STDERR)

    with pytest.raises(RuntimeError, match="must not be retried"):
        client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert calls["n"] == 1


def test_cli_precondition_failure_surfaces_conflict_to_a_cas_caller(monkeypatch):
    """A conditional claim losing on the CLI path must still reach
    `task_claim`'s handler as `OmnigraphConflict`, not a bare RuntimeError."""
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr=_PRECONDITION_STDERR)

    with pytest.raises(OmnigraphConflict, match="precondition_failure"):
        client._execute(
            ["omnigraph", "mutate"], "mutate", is_write=True, surface_conflict=True
        )
    assert calls["n"] == 1


def test_cli_recovery_required_on_a_write_is_indeterminate(monkeypatch):
    """The branch-wide barrier, on the path where only the prose identifies it.

    Terminal for a write, and typed: the outcome genuinely is unknown, so the
    caller must re-read rather than be handed a retry that might duplicate.
    """
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr=_RECOVERY_STDERR)

    with pytest.raises(og.WriteIndeterminate, match="INDETERMINATE"):
        client._execute(["omnigraph", "mutate"], "mutate", is_write=True)
    assert calls["n"] == 1


def test_cli_recovery_required_on_a_read_retries(monkeypatch):
    """…but a read repeats harmlessly, and the barrier does clear on its own.

    Classified FATAL (the behaviour before this change) a reader caught behind
    someone else's barrier got a permanent-looking error for a condition that
    resolves in under a second.
    """
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr=_RECOVERY_STDERR)

    with pytest.raises(RuntimeError):
        client._execute(["omnigraph", "query"], "query", is_write=False)
    assert calls["n"] > 1


def test_cli_change_never_sends_json_and_returns_no_commit(monkeypatch):
    """★ THE GUARDRAIL, NOT JUST A GAP. `--json` was deliberately NOT added to
    close this gap on the CLI path: verified empirically against the real CLI,
    2026-08-18, a lost `--if-commit` race reports its failure differently
    depending on `--json` — WITHOUT it the message is on stderr (what
    `_classify_cli_error`'s `_PRECONDITION_FAILED` markers are tuned against);
    WITH it, stderr comes back EMPTY and the failure moves entirely to a JSON
    body on stdout. `_execute` classifies from `result.stderr` only, so `--json`
    here would silently starve that classifier on every CLI-path precondition
    failure. This is a regression guard on the ABSENCE of a flag — if this test
    ever needs to change because someone adds `--json`, read this comment and
    the one on `change()` in omnigraph.py before touching it.
    """
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    _force_cli(monkeypatch)
    monkeypatch.setattr(og.subprocess, "run", fake_run)
    client = _built_client(monkeypatch, "http://host:8080", graph_id="council")

    new_commit = client.change("mutations.gq", "claim", {"slug": "t-1"})

    assert "--json" not in captured["cmd"]
    assert new_commit is None


def test_write_authority_conflict_is_retried_when_not_surfaced(monkeypatch):
    """An ordinary writer that merely lost a race should try again, not die.

    This is the non-CAS half of the same defect: every plain `memory_store` that
    raced another writer failed outright where a retry would have committed.
    """
    client = _client(monkeypatch)
    calls = _stub_run(monkeypatch, returncode=1, stderr=_WRITE_AUTHORITY_STDERR)

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


def test_a_second_thread_still_blocks_on_a_lock_this_thread_holds(tmp_path):
    """Re-entrancy is per THREAD, and only a second thread can prove it.

    Every other lock test here nests within one thread, which a process-wide
    depth counter would satisfy just as well — while handing a second thread a
    lock the first is holding, destroying the mutual exclusion the lock exists
    for. That wrong implementation is indistinguishable from the right one
    until two threads contend, so this is the test that pins the choice.

    Written so it can only fail by re-entrancy being wrongly GRANTED: the
    contender announces itself before attempting, and being slow merely makes
    the negative check weaker, never false.
    """
    store = str(tmp_path / "graph.omni")
    attempting = threading.Event()
    acquired = threading.Event()
    may_release = threading.Event()
    released = threading.Event()
    got = {}

    def contender():
        attempting.set()
        got["fh"] = acquire = og.acquire_store_flock(store)
        acquired.set()
        may_release.wait(timeout=10)
        # Released on the acquiring thread: the registry is keyed by thread, so
        # releasing from the main thread would leave this entry behind.
        og.release_store_flock(store, acquire)
        released.set()

    outer = og.acquire_store_flock(store)  # held by the MAIN thread
    thread = threading.Thread(target=contender, daemon=True)
    thread.start()
    assert attempting.wait(timeout=10)

    # The whole point: another thread must NOT get in behind our back.
    assert not acquired.wait(timeout=0.5)

    og.release_store_flock(store, outer)

    assert acquired.wait(timeout=10)
    # And it took a real lock of its own, not a re-entrancy free pass.
    assert got["fh"] is not None

    may_release.set()
    assert released.wait(timeout=10)
    thread.join(timeout=10)
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


# ── client-side write admission (remote stores only) ───────────────
#
# MEASURED 2026-08-12 against the CI data tier, N threads released from a
# barrier: n=4 concurrent single-row writes take 15.5s wall, n=6 take 31.2s,
# n=8 take 51.1s with a median of 33.3s. The tool call carrying them is cut at
# 30s. So past ~4 in flight per graph, admitting a write means producing a 502
# whose outcome nobody can determine — the gate refuses instead, BEFORE
# anything is sent, which is the one answer a 502 can never give.
#
# The data tier's own cap cannot do this job: it is per ACTOR, and the deadline
# sees total concurrency. Ten users at one write each satisfies every per-actor
# cap and still blows it. This process is the single point every user's write
# passes through, so it is the only place the total is visible.


class _StubTransport:
    """A transport whose mutate/query block until released, and count calls."""

    def __init__(self, server_url="https://graph.example"):
        self.server_url = server_url
        self.calls: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def _respond(self, verb):
        self.calls.append(verb)
        self.entered.set()
        self.release.wait(5)
        return og._http.Outcome(kind=og._http.OK, body='{"rows": []}')

    def mutate(self, graph_id, source, params, token):
        return self._respond("mutate")

    def query(self, graph_id, source, params, token):
        return self._respond("query")


def _write(client, transport, label="mutate"):
    return client._http_execute(transport, "mutate", "query x() {}", {}, label)


def test_a_write_past_the_bound_is_refused_before_it_is_sent(monkeypatch):
    """★ The whole point: a refusal, not a torn-down connection.

    The bound is monkeypatched to 1, which also pins that it is read at CALL
    time rather than captured at import — the gate is a module-level singleton
    built long before this test runs.
    """
    monkeypatch.setattr(og, "_REMOTE_WRITE_MAX_INFLIGHT", 1)
    monkeypatch.setattr(og, "_REMOTE_WRITE_QUEUE_WAIT", 0.05)
    client = _client(monkeypatch)
    transport = _StubTransport()
    transport.release.clear()

    holder = threading.Thread(target=_write, args=(client, transport), daemon=True)
    holder.start()
    assert transport.entered.wait(5), "the first write never reached the transport"

    with pytest.raises(og.WriteQueueFull) as caught:
        _write(client, transport)

    message = str(caught.value)
    assert "NOTHING WAS WRITTEN" in message
    assert "1 writes are already in flight" in message
    # ★ The refusal is what a 502 cannot be: provably pre-send.
    assert transport.calls == ["mutate"]

    transport.release.set()
    holder.join(5)


def test_the_cli_write_path_is_gated_too(monkeypatch):
    """★ THE BOUND MUST NOT BE WALKABLE-AROUND BY CHOOSING A TRANSPORT.

    Gating only ``_http_execute`` left two real remote-write paths unbounded,
    and both are ones a burst actually travels: ``load_batch`` shells out to
    ``omnigraph load`` (there is no HTTP form of it), so every batch of a
    ``store_merge`` escaped — two people migrating at once being exactly the
    multi-user burst this exists to bound — and witan-code's branched clients
    fall back to the CLI by design, so every write from a branch view escaped
    as well.

    Driven through ``_run`` rather than a transport, so it fails if admission
    ever moves back to the HTTP branch.
    """
    monkeypatch.setattr(og, "_REMOTE_WRITE_MAX_INFLIGHT", 0)
    monkeypatch.setattr(og, "_REMOTE_WRITE_QUEUE_WAIT", 0.05)
    client = _client(monkeypatch)
    ran = {"n": 0}

    def fake_run(cmd, **kwargs):
        ran["n"] += 1
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(og.WriteQueueFull):
        client._run("mutate", "--source", "x")
    assert ran["n"] == 0, "refused writes must not reach the subprocess"

    # A READ over the same transport is ungated — reads do not queue.
    client._run("query", "--source", "x")
    assert ran["n"] == 1


def test_a_local_store_write_is_not_gated(monkeypatch, tmp_path):
    """The bound is about saturating a shared server. A local store has none —
    it is serialised by its own flock — so gating it would only add latency."""
    monkeypatch.setattr(og, "_REMOTE_WRITE_MAX_INFLIGHT", 0)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    client = OmnigraphClient(str(tmp_path / "g.omni"), Path("/queries"), graph_id="g")
    ran = {"n": 0}

    def fake_run(cmd, **kwargs):
        ran["n"] += 1
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client._run("mutate", "--source", "x")
    assert ran["n"] == 1


def test_the_slot_is_released_so_writes_queue_rather_than_fail(monkeypatch):
    monkeypatch.setattr(og, "_REMOTE_WRITE_MAX_INFLIGHT", 1)
    client = _client(monkeypatch)
    transport = _StubTransport()

    for _ in range(3):
        _write(client, transport)
    assert transport.calls == ["mutate"] * 3


def test_the_slot_is_released_even_when_the_write_fails(monkeypatch):
    """A failed write that kept its slot would wedge the graph after N failures."""
    monkeypatch.setattr(og, "_REMOTE_WRITE_MAX_INFLIGHT", 1)
    monkeypatch.setattr(og, "_REMOTE_WRITE_QUEUE_WAIT", 0.05)
    client = _client(monkeypatch)

    class _Failing(_StubTransport):
        def mutate(self, graph_id, source, params, token):
            self.calls.append("mutate")
            return og._http.Outcome(kind=og._http.FATAL, error="denied")

    transport = _Failing()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="denied"):
            _write(client, transport)
    assert transport.calls == ["mutate"] * 2


def test_reads_are_not_gated(monkeypatch):
    """Reads hold flat at ~5 req/s from 8 to 36 concurrent readers — they do not
    serialise, so gating them would add latency to the one path that has none."""
    monkeypatch.setattr(og, "_REMOTE_WRITE_MAX_INFLIGHT", 0)
    client = _client(monkeypatch)
    transport = _StubTransport()

    client._http_execute(transport, "query", "query x() {}", {}, "read")
    assert transport.calls == ["query"]
    # ...and the same client's write is refused outright at that bound.
    with pytest.raises(og.WriteQueueFull):
        _write(client, transport)


def test_two_graphs_do_not_block_each_other(monkeypatch):
    """★ PER GRAPH, not per server. Measured: 4 writes to `code-bridge` and 4 to
    `code-…-lehrer` fired together each finished in their solo time. A repo's
    reindex must not be able to stall a memory write."""
    monkeypatch.setattr(og, "_REMOTE_WRITE_MAX_INFLIGHT", 1)
    monkeypatch.setattr(og, "_REMOTE_WRITE_QUEUE_WAIT", 0.05)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    council = OmnigraphClient("https://graph.example", Path("/q"), graph_id="council")
    code = OmnigraphClient("https://graph.example", Path("/q"), graph_id="code-bridge")
    transport = _StubTransport()
    transport.release.clear()

    holder = threading.Thread(target=_write, args=(council, transport), daemon=True)
    holder.start()
    assert transport.entered.wait(5)

    # The other graph is admitted immediately rather than waiting out the bound.
    transport.release.set()
    _write(code, transport)
    assert transport.calls == ["mutate", "mutate"]
    holder.join(5)


def test_the_bound_is_settable_from_the_environment(monkeypatch):
    """★ Retuning a deployment must not need a release.

    These numbers came from one measurement of one data tier and will move when
    the per-write cost does. A bound that can only be changed by editing code,
    cutting a release and rebuilding an image is a bound nobody can correct
    during an incident.
    """
    monkeypatch.setenv(og.REMOTE_WRITE_MAX_INFLIGHT_ENV_VAR, "1")
    monkeypatch.setenv(og.REMOTE_WRITE_QUEUE_WAIT_ENV_VAR, "0.05")
    client = _client(monkeypatch)
    transport = _StubTransport()
    transport.release.clear()

    holder = threading.Thread(target=_write, args=(client, transport), daemon=True)
    holder.start()
    assert transport.entered.wait(5)

    # The module default is 4; the environment says 1, and the environment wins.
    with pytest.raises(og.WriteQueueFull):
        _write(client, transport)

    transport.release.set()
    holder.join(5)


@pytest.mark.parametrize("value", ["not-a-number", "-1", ""])
def test_an_unusable_env_override_falls_back_instead_of_raising(monkeypatch, value):
    """A typo in a Deployment env var must not turn every write into a crash —
    that is the one failure worse than the mis-sizing it was correcting."""
    monkeypatch.setenv(og.REMOTE_WRITE_MAX_INFLIGHT_ENV_VAR, value)
    assert (
        og._env_override(og.REMOTE_WRITE_MAX_INFLIGHT_ENV_VAR, 4, int) == 4
    )  # the default, not an exception


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "Infinity", "NaN"])
def test_a_non_finite_env_override_falls_back(monkeypatch, value):
    """★ The one bad value that passes every other check.

    ``float()`` accepts these, and ``nan`` compares False against everything —
    so ``nan < 0`` is False and a negative-only guard waves it straight through
    to ``Condition.wait(nan)``, which raises out of the gate. That is exactly
    the "a bad env var turns every write into a crash" failure the fallback
    exists to prevent, arriving through the input nobody type-checks.
    """
    monkeypatch.setenv(og.REMOTE_WRITE_QUEUE_WAIT_ENV_VAR, value)
    assert og._env_override(og.REMOTE_WRITE_QUEUE_WAIT_ENV_VAR, 10.0, float) == 10.0


def test_a_non_finite_queue_wait_does_not_break_the_gate(monkeypatch):
    """The end-to-end form of the above: the gate still refuses cleanly with
    ``WriteQueueFull`` rather than raising ValueError/OverflowError out of
    ``Condition.wait``."""
    monkeypatch.setenv(og.REMOTE_WRITE_QUEUE_WAIT_ENV_VAR, "nan")
    monkeypatch.setenv(og.REMOTE_WRITE_MAX_INFLIGHT_ENV_VAR, "0")
    gate = og._WriteGate()
    with pytest.raises(og.WriteQueueFull):
        with gate.admit("graph", "mutate"):
            pass  # pragma: no cover — admission must not succeed at limit 0


def test_zero_disables_admission_rather_than_reading_as_unset(monkeypatch):
    """0 is a legal operational answer (refuse every remote write), and it must
    not be mistaken for "no value set" — which would silently re-enable them."""
    monkeypatch.setenv(og.REMOTE_WRITE_MAX_INFLIGHT_ENV_VAR, "0")
    monkeypatch.setenv(og.REMOTE_WRITE_QUEUE_WAIT_ENV_VAR, "0")
    client = _client(monkeypatch)
    transport = _StubTransport()

    with pytest.raises(og.WriteQueueFull):
        _write(client, transport)
    assert transport.calls == []


# ── the server's own Retry-After (HTTP transport only) ─────────────


def _capped(retry_after):
    return og._http.Outcome(
        kind=og._http.ADMISSION_CAP,
        error="actor in-flight count cap exceeded",
        status=429,
        retry_after=retry_after,
    )


def test_a_short_retry_after_replaces_the_blind_schedule(monkeypatch):
    client = _client(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(og.time, "sleep", lambda s: sleeps.append(s))
    outcomes = [_capped(1.5), og._http.Outcome(kind=og._http.OK, body="ok")]

    class _Transport(_StubTransport):
        def mutate(self, graph_id, source, params, token):
            return outcomes.pop(0)

    assert _write(client, _Transport()) == "ok"
    assert sleeps == [1.5]  # the server's number, not 0.25 * 2**n


def test_a_retry_after_longer_than_the_call_can_wait_fails_immediately(monkeypatch):
    """★ Obeying it is not an option, so quoting it is the honest answer.

    Only when the deployment has SAID it has a deadline. Given a 30s budget, a
    60s sleep can only end as a torn-down connection whose outcome the caller
    cannot determine. Failing now — with the server's own number in the message
    — hands the wait to whoever schedules the retry instead.
    """
    monkeypatch.setenv(og.REMOTE_CALL_BUDGET_ENV_VAR, "30")
    client = _client(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(og.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    class _Transport(_StubTransport):
        def mutate(self, graph_id, source, params, token):
            calls["n"] += 1
            return _capped(60.0)

    with pytest.raises(RuntimeError, match="retry in 60s"):
        _write(client, _Transport())
    assert calls["n"] == 1  # not six attempts of sleeping through a wall
    assert sleeps == []


def test_a_retry_after_that_fits_the_budget_is_obeyed(monkeypatch):
    """★ 5s is longer than the per-sleep backoff cap and still fits in 30s.

    The regression this pins: the threshold used to be
    ``_ADMISSION_CAP_MAX_DELAY`` (4.0), which is how long ONE backoff sleep may
    be — not how long the call has left. Measured against that, a perfectly
    affordable ``Retry-After: 5`` was refused outright.
    """
    monkeypatch.setenv(og.REMOTE_CALL_BUDGET_ENV_VAR, "30")
    client = _client(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(og.time, "sleep", lambda s: sleeps.append(s))
    outcomes = [_capped(5.0), og._http.Outcome(kind=og._http.OK, body="ok")]

    class _Transport(_StubTransport):
        def mutate(self, graph_id, source, params, token):
            return outcomes.pop(0)

    assert _write(client, _Transport()) == "ok"
    assert sleeps == [5.0]


def test_repeated_affordable_hints_stop_at_the_call_budget(monkeypatch):
    """★ The other half of the same conflation, and the dangerous half.

    Each ``Retry-After: 4`` is individually affordable, so a per-sleep test
    admits every one of them and the total marches past the cut-off it meant to
    stay inside — the write gate's own queue wait having already eaten part of
    the same budget. Against one absolute deadline the SEQUENCE stops, and it
    stops on the budget rather than on ``_ADMISSION_CAP_MAX_ATTEMPTS``: the
    remaining budget here (15s) runs out at the fourth hint, well before the
    sixth attempt.
    """
    monkeypatch.setenv(og.REMOTE_CALL_BUDGET_ENV_VAR, "15")
    client = _client(monkeypatch)
    now = {"t": 0.0}
    monkeypatch.setattr(og.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(og.time, "sleep", lambda s: now.__setitem__("t", now["t"] + s))

    class _Transport(_StubTransport):
        def mutate(self, graph_id, source, params, token):
            return _capped(4.0)

    with pytest.raises(RuntimeError, match="longer than the"):
        _write(client, _Transport())
    assert now["t"] <= 15.0  # never slept past the budget it was given


def test_without_a_declared_budget_any_hint_is_obeyed(monkeypatch):
    """★ THE LAYERING GUARANTEE: witan-core assumes no deadline of its own.

    The same client runs behind a hosting layer that cuts requests, from an
    interactive CLI that does not, and from a batch Job happy to wait minutes.
    Only the deployment knows which, so with the budget unset a 60s hint is
    obeyed rather than refused — no hosting layer's timeout is baked in here.
    """
    monkeypatch.delenv(og.REMOTE_CALL_BUDGET_ENV_VAR, raising=False)
    client = _client(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(og.time, "sleep", lambda s: sleeps.append(s))
    outcomes = [_capped(60.0), og._http.Outcome(kind=og._http.OK, body="ok")]

    class _Transport(_StubTransport):
        def mutate(self, graph_id, source, params, token):
            return outcomes.pop(0)

    assert _write(client, _Transport()) == "ok"
    assert sleeps == [60.0]


def test_no_retry_after_keeps_the_blind_schedule(monkeypatch):
    """The CLI path never has the header — its backoff must be unchanged."""
    client = _client(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(og.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(og.random, "uniform", lambda a, b: 0.0)
    outcomes = [_capped(None), og._http.Outcome(kind=og._http.OK, body="ok")]

    class _Transport(_StubTransport):
        def mutate(self, graph_id, source, params, token):
            return outcomes.pop(0)

    assert _write(client, _Transport()) == "ok"
    assert sleeps == [og._ADMISSION_CAP_BASE_DELAY]


# ── admission by predicted completion, not by slot availability ────────────
# MEASURED 2026-08-13 at 24 writers: 56 handlers, durations climbing 3s -> 73s,
# 26 of them past the caller's 30s deadline, and ZERO refusals — a slot always
# freed inside the 10s wait cap, so the gate admitted write after write into a
# system that could not finish them. Bounding concurrency never asked the only
# question that matters: will this finish in time?


def _gate_with(estimate: float | None, in_flight: int = 1) -> og._WriteGate:
    """A gate that already believes writes to `g` cost `estimate` seconds."""
    gate = og._WriteGate()
    if estimate is not None:
        gate._service["g"] = estimate
    if in_flight:
        gate._in_flight["g"] = in_flight
    return gate


def test_a_write_that_cannot_finish_in_the_budget_is_refused_before_sending():
    """★ THE REGRESSION. 40s of measured service against 10s of remaining
    budget used to be admitted — and a write admitted here is one the caller is
    later told failed while it committed anyway."""
    gate = _gate_with(40.0)
    with pytest.raises(og.WriteQueueFull) as caught:
        with gate.admit("g", "mutate", call_deadline=time.monotonic() + 10):
            pass  # pragma: no cover — admission must not succeed
    message = str(caught.value)
    assert "cannot complete in time" in message
    assert "NOTHING WAS WRITTEN" in message


def test_a_write_that_fits_is_admitted():
    gate = _gate_with(4.0)
    with gate.admit("g", "mutate", call_deadline=time.monotonic() + 30):
        pass
    assert gate._service["g"] > 0


def test_an_idle_graph_is_never_refused():
    """★ A DEADLOCK DRESSED AS BACKPRESSURE. If nothing is in flight this write
    is all the graph has to do, and refusing it would decline every write
    forever — the estimate could never be refreshed by a faster sample."""
    gate = _gate_with(600.0, in_flight=0)
    with gate.admit("g", "mutate", call_deadline=time.monotonic() + 1):
        pass


def test_a_cold_gate_admits_because_it_has_nothing_to_predict_from():
    """A process with no samples must not refuse the very writes it needs in
    order to learn what a write costs."""
    gate = _gate_with(None)
    with gate.admit("g", "mutate", call_deadline=time.monotonic() + 1):
        pass


def test_queue_time_is_charged_against_the_budget():
    """The deadline is absolute, so seconds spent waiting for a slot shrink what
    the estimate is checked against — no separate accounting needed."""
    gate = _gate_with(5.0)
    already_spent = time.monotonic() - 26  # 26s of a 30s budget gone
    with pytest.raises(og.WriteQueueFull):
        with gate.admit("g", "mutate", call_deadline=already_spent + 30):
            pass  # pragma: no cover


def test_no_deadline_means_no_predictive_refusal():
    """The CLI path declares no budget; it must behave as it always did."""
    gate = _gate_with(600.0)
    with gate.admit("g", "mutate", call_deadline=None):
        pass


def test_the_estimate_measures_execution_and_not_the_wait(monkeypatch):
    """★ THE SUBTLETY THAT WOULD MAKE THIS RUN AWAY. Folding queue time into the
    estimate double-counts it: the queue inflates the estimate, the estimate
    refuses more, and one burst leaves the gate believing every write costs a
    minute. The observed 3s -> 73s spread is mostly queue, not service.

    The waiter must ACTUALLY QUEUE for this to prove anything, so the gate is
    filled to a limit of one and the holder is released only after the waiter
    is provably blocked on the condition variable. An earlier version of this
    test released the holder first and admitted into a gate with a free slot —
    it measured a near-zero body without ever touching the wait boundary, and
    so could not have caught the regression it names.

    Asserted on what `_record` was HANDED, not on the resulting EWMA: the
    holder's own long body lands in the same average, so the average cannot
    distinguish "the waiter recorded its wait" from "the holder was slow".
    """
    monkeypatch.setattr(og, "_REMOTE_WRITE_MAX_INFLIGHT", 1)
    gate = og._WriteGate()
    recorded: list[tuple[str, float]] = []
    real_record = gate._record
    monkeypatch.setattr(
        gate,
        "_record",
        lambda k, e: (recorded.append((k, e)), real_record(k, e))[1],
    )

    holding = threading.Event()
    may_release = threading.Event()

    def holder():
        with gate.admit("g", "mutate"):
            holding.set()
            may_release.wait(5)

    h = threading.Thread(target=holder, daemon=True)
    h.start()
    assert holding.wait(5), "holder never took the slot"

    waiter_elapsed: list[float] = []

    def waiter():
        started = time.monotonic()
        with gate.admit("g", "mutate", call_deadline=time.monotonic() + 30):
            pass
        waiter_elapsed.append(time.monotonic() - started)

    w = threading.Thread(target=waiter, daemon=True)
    w.start()
    # Give the waiter time to reach `_cv.wait` — it cannot proceed while the
    # holder occupies the only slot.
    time.sleep(0.4)
    assert not waiter_elapsed, "waiter did not queue; the gate had a free slot"
    may_release.set()
    h.join(5)
    w.join(5)

    assert waiter_elapsed and waiter_elapsed[0] > 0.3, "waiter should have waited"
    # Two records: the holder's long body, then the waiter's near-zero one. The
    # waiter's is the one that must exclude the 0.4s it spent queueing.
    waiter_record = recorded[-1][1]
    assert waiter_record < 0.2, (
        f"the waiter recorded {waiter_record:.2f}s after waiting "
        f"{waiter_elapsed[0]:.2f}s — queue time is leaking into the estimate"
    )


def test_the_estimate_rises_faster_than_it_falls():
    """Quick to believe a slowdown, slow to forget one — the failure being
    prevented is admitting DURING a slowdown."""
    gate = og._WriteGate()
    gate._record("g", 10.0)
    gate._record("g", 20.0)
    after_rise = gate._service["g"]
    gate._record("g", 10.0)
    after_fall = gate._service["g"]
    assert after_rise > 14.0, "a slowdown must move the estimate sharply"
    assert after_fall > 13.0, "a single fast sample must not erase it"


def test_a_doomed_write_is_refused_immediately_not_after_the_queue_timeout(
    monkeypatch,
):
    """★ REFUSING LATE IS ITSELF THE BUG. Asking only after the queue cleared
    let a write sit the full wait — burning the caller's budget to reach a
    verdict that was already true when it arrived. The point of the gate is to
    stop consuming a deadline that cannot be met.
    """
    monkeypatch.setattr(og, "_REMOTE_WRITE_MAX_INFLIGHT", 1)
    monkeypatch.setattr(og, "_REMOTE_WRITE_QUEUE_WAIT", 30.0)  # long on purpose
    gate = og._WriteGate()
    gate._service["g"] = 40.0  # writes cost far more than the budget below
    gate._in_flight["g"] = 1  # and the gate is full, so it would queue

    started = time.monotonic()
    with pytest.raises(og.WriteQueueFull):
        with gate.admit("g", "mutate", call_deadline=time.monotonic() + 10):
            pass  # pragma: no cover
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, (
        f"refused after {elapsed:.1f}s — it waited on a queue it could never "
        "have used in time"
    )


def test_the_wait_is_capped_at_the_last_viable_moment(monkeypatch):
    """`Condition.wait` does not wake when the budget runs out, so an unbounded
    wait sleeps past its own deadline and is then refused for having waited.
    The wait is bounded by `call_deadline - estimate`."""
    monkeypatch.setattr(og, "_REMOTE_WRITE_MAX_INFLIGHT", 1)
    monkeypatch.setattr(og, "_REMOTE_WRITE_QUEUE_WAIT", 30.0)
    gate = og._WriteGate()
    gate._service["g"] = 1.0
    gate._in_flight["g"] = 1

    # 1.5s of budget against a 1.0s write: viable for ~0.5s, then hopeless.
    started = time.monotonic()
    with pytest.raises(og.WriteQueueFull):
        with gate.admit("g", "mutate", call_deadline=time.monotonic() + 1.5):
            pass  # pragma: no cover
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, "should give up near the viable boundary, not at 30s"
    assert elapsed > 0.2, "should have waited while admission was still viable"


# ── omnigraph 0.10.0's full-text rebuild guard (upstream #581) ──────────────
#
# Literal, for the reason `_WRITE_AUTHORITY_STDERR` is: taken from the `#[error]`
# attribute on `OmniError::FullTextIndexRebuildRequired` in
# crates/omnigraph/src/error.rs at v0.10.0.
_FTS_REBUILD_STDERR = (
    "omnigraph query failed:\n"
    "full-text index 'memory_content' requires rebuild: analyzer generation "
    "cannot be proven compatible; run omnigraph rebuild-full-text-indexes "
    "<URI> --branch <branch> on the live branch (historical snapshots are "
    "unchanged)"
)


def test_full_text_rebuild_required_is_terminal_on_the_cli_path():
    """Lance 11 changed the analyzer, so an index built by Lance 10 cannot serve
    a 0.10.0 `search()`/`bm25()` query. Retrying never clears it — upstream says
    so outright — so classifying it RETRYABLE would burn the whole attempt
    budget and then report a timeout-shaped failure for a permanent condition
    whose remedy is printed right there in the message.
    """
    assert (
        og._classify_cli_error(_FTS_REBUILD_STDERR) == _http.FULL_TEXT_REBUILD_REQUIRED
    )


def test_full_text_rebuild_is_not_confused_with_a_repair():
    """NEEDS_REPAIR would run `omnigraph repair --force` on a graph that is not
    damaged. Upstream is explicit that ordinary reads stay available and only
    the full-text index is refused."""
    assert og._classify_cli_error(_FTS_REBUILD_STDERR) != _http.NEEDS_REPAIR
