"""Tests for the parts of docker/omnigraph_server_entrypoint.py the fakes hide.

test_omnigraph_server_entrypoint.py replaces `KubernetesApi` with a fake, so a
wrong URL, a token read once instead of per request, or a 409 that is not
reported as one would leave the force-unlock gate unusable with every test
there passing. These run the real client against a local TLS server that
trusts only the ServiceAccount CA, and cover `main`, and the two unlock
branches the scripted CLI did not reach.
"""

from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from .test_omnigraph_server_entrypoint import (
    LEASE,
    POD,
    FakeKubernetes,
    FakeOmnigraph,
    dialect,  # noqa: F401  (fixture)
    entrypoint,
)

BootRefused = entrypoint.BootRefused
KubernetesError = entrypoint.KubernetesError

NAMESPACE = "witan"

requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="needs openssl to make a test CA"
)


class ApiServer:
    """A TLS server that records each request and answers from a script."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.requests: list[dict] = []
        self.responses: list[tuple[int, object]] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                recorder.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": json.loads(self.rfile.read(length)) if length else None,
                    }
                )
                status, payload = (
                    recorder.responses.pop(0) if recorder.responses else (200, {})
                )
                body = (
                    payload
                    if isinstance(payload, bytes)
                    else json.dumps(payload).encode()
                )
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_PUT = _answer

            def log_message(self, *args: object) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def make_ca(directory: Path) -> tuple[Path, Path]:
    cert, key = directory / "ca.crt", directory / "tls.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=kubernetes",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.fixture
def service_account(tmp_path) -> Path:
    """A projected ServiceAccount volume, with a CA only the test server has."""
    directory = tmp_path / "serviceaccount"
    directory.mkdir()
    make_ca(directory)
    (directory / "token").write_text("first-token\n")
    (directory / "namespace").write_text(f"{NAMESPACE}\n")
    return directory


@pytest.fixture
def server(service_account):
    api_server = ApiServer(service_account / "ca.crt", service_account / "tls.key")
    yield api_server
    api_server.close()


@pytest.fixture
def api(server, service_account):
    return entrypoint.KubernetesApi(
        f"https://127.0.0.1:{server.port}", NAMESPACE, service_account
    )


# ── KubernetesApi ────────────────────────────────────────────────────────────


@requires_openssl
def test_get_lease_addresses_the_named_lease_in_this_namespace(api, server):
    server.responses.append((200, {"metadata": {"resourceVersion": "7"}}))
    assert api.get_lease(LEASE) == {"metadata": {"resourceVersion": "7"}}
    assert server.requests == [
        {
            "method": "GET",
            "path": f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases/{LEASE}",
            "authorization": "Bearer first-token",
            "body": None,
        }
    ]


@requires_openssl
def test_replace_lease_puts_the_object_it_was_given(api, server):
    lease = {"metadata": {"resourceVersion": "7"}, "spec": {"holderIdentity": "me"}}
    api.replace_lease(LEASE, lease)
    (request,) = server.requests
    assert (request["method"], request["body"]) == ("PUT", lease)
    assert request["path"].endswith(f"/leases/{LEASE}")


@requires_openssl
def test_the_token_is_read_again_for_every_request(api, server, service_account):
    """The kubelet rotates the projected token file in place."""
    api.get_lease(LEASE)
    (service_account / "token").write_text("rotated-token\n")
    api.get_lease(LEASE)
    assert [request["authorization"] for request in server.requests] == [
        "Bearer first-token",
        "Bearer rotated-token",
    ]


@requires_openssl
def test_list_pods_encodes_the_selector_and_returns_the_items(api, server):
    server.responses.append((200, {"items": [{"metadata": {"name": POD}}]}))
    selector = "app.kubernetes.io/name=omnigraph-server,tier in (data)"
    assert api.list_pods(selector) == [{"metadata": {"name": POD}}]
    assert server.requests[0]["path"] == (
        f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector="
        "app.kubernetes.io%2Fname%3Domnigraph-server%2Ctier+in+%28data%29"
    )


@requires_openssl
def test_the_scale_write_carries_the_deployments_resource_version(api, server):
    api.get_deployment("omnigraph-server")
    api.replace_scale("omnigraph-server", 0, "4242")
    read, write = server.requests
    path = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/omnigraph-server"
    assert (read["method"], read["path"]) == ("GET", path)
    assert (write["method"], write["path"]) == ("PUT", f"{path}/scale")
    assert write["body"] == {
        "apiVersion": "autoscaling/v1",
        "kind": "Scale",
        "metadata": {
            "name": "omnigraph-server",
            "namespace": NAMESPACE,
            "resourceVersion": "4242",
        },
        "spec": {"replicas": 0},
    }


@requires_openssl
@pytest.mark.parametrize("status", [403, 404, 409, 500])
def test_an_http_error_keeps_its_status(api, server, status):
    """The Lease code tells a lost write (409) from an unreachable API by it."""
    server.responses.append((status, {"kind": "Status", "reason": "nope"}))
    with pytest.raises(KubernetesError, match=f"HTTP {status}.*nope") as raised:
        api.replace_lease(LEASE, {})
    assert raised.value.status == status


@requires_openssl
def test_a_response_that_is_not_json_is_an_error_with_no_status(api, server):
    server.responses.append((200, b"<html>proxy</html>"))
    with pytest.raises(KubernetesError) as raised:
        api.get_lease(LEASE)
    assert raised.value.status is None


@requires_openssl
def test_an_unreachable_api_is_an_error_with_no_status(api, server):
    server.close()
    with pytest.raises(KubernetesError) as raised:
        api.get_lease(LEASE)
    assert raised.value.status is None


@requires_openssl
def test_a_server_the_service_account_ca_did_not_sign_is_refused(
    server, service_account, tmp_path
):
    other = tmp_path / "other"
    other.mkdir()
    make_ca(other)
    (other / "token").write_text("first-token")
    api = entrypoint.KubernetesApi(f"https://127.0.0.1:{server.port}", NAMESPACE, other)
    with pytest.raises(KubernetesError, match="CERTIFICATE_VERIFY_FAILED") as raised:
        api.get_lease(LEASE)
    assert raised.value.status is None
    assert server.requests == []


@requires_openssl
@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("10.100.0.1", "https://10.100.0.1:443"),
        ("fd00:10:96::1", "https://[fd00:10:96::1]:443"),
    ],
)
def test_in_cluster_builds_the_url_from_the_service_environment(
    monkeypatch, service_account, host, url
):
    monkeypatch.setattr(entrypoint, "SERVICE_ACCOUNT_DIR", service_account)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", host)
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    api = entrypoint.KubernetesApi.in_cluster()
    assert api._base_url == url
    assert api._namespace == NAMESPACE
    assert api._token_file == service_account / "token"


# ── main ─────────────────────────────────────────────────────────────────────


class Exec(Exception):
    """`os.execvp` was reached."""


@pytest.fixture
def execvp(monkeypatch) -> list:
    calls: list = []

    def record(binary: str, argv: list[str]) -> None:
        calls.append((binary, argv))
        raise Exec

    monkeypatch.setattr(entrypoint.os, "execvp", record)
    return calls


def test_main_converges_then_execs_the_server_with_its_arguments(
    monkeypatch,
    execvp,
    tmp_path,
    dialect,  # noqa: F811
):
    fake = FakeOmnigraph(dialect)
    monkeypatch.setattr(entrypoint, "run_omnigraph", fake)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("OMNIGRAPH_UNLOCK_WITHOUT_PEERS", "1")
    argv = ["--cluster", str(tmp_path), "--bind", "0.0.0.0:8080"]
    with pytest.raises(Exec):
        entrypoint.main(argv)
    lock = dialect.lock_command
    assert fake.commands == ["plan", lock, "apply", lock, "force-unlock"]
    assert execvp == [("omnigraph-server", ["omnigraph-server", *argv])]


def test_main_exits_1_without_starting_the_server_when_the_boot_is_refused(
    monkeypatch,
    execvp,
    tmp_path,
    capsys,
    dialect,  # noqa: F811
):
    fake = FakeOmnigraph(
        dialect, lock={"id": "01MAINTENANCE", "operation": "graph_operation"}
    )
    monkeypatch.setattr(entrypoint, "run_omnigraph", fake)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with pytest.raises(SystemExit) as exited:
        entrypoint.main(["--cluster", str(tmp_path)])
    assert exited.value.code == 1
    assert execvp == []
    assert "refusing to start" in capsys.readouterr().err


def test_main_does_not_converge_a_storage_uri(monkeypatch, execvp):
    def unexpected(*args: object) -> None:
        raise AssertionError(args)

    monkeypatch.setattr(entrypoint, "run_omnigraph", unexpected)
    with pytest.raises(Exec):
        entrypoint.main(["--cluster=s3://bucket/root"])
    assert execvp == [
        ("omnigraph-server", ["omnigraph-server", "--cluster=s3://bucket/root"])
    ]


# ── The unlock after apply ───────────────────────────────────────────────────


class ApplyHook(FakeOmnigraph):
    """Runs ``after_apply`` once the apply has left its lock."""

    after_apply = None

    def __call__(self, args: list[str], lease, timeout_seconds=None) -> object:
        result = super().__call__(args, lease)
        if args[1] == "apply" and self.after_apply:
            self.after_apply()
        return result


def test_a_lease_lost_between_the_two_unlocks_leaves_the_apply_lock(
    monkeypatch,
    tmp_path,
    dialect,  # noqa: F811
):
    """Held for the first unlock is not held for the second."""
    kube = FakeKubernetes()
    lease = entrypoint.UnlockLease(kube, LEASE, POD)
    lease.acquire()
    fake = ApplyHook(dialect, lock={"id": "01PREVIOUS", "operation": "serve"})
    fake.after_apply = lambda: kube.hold_as("operator#aa", renewed_ago=0)
    monkeypatch.setattr(entrypoint, "run_omnigraph", fake)
    try:
        with pytest.raises(BootRefused, match="lost the omnigraph-unlock Lease"):
            entrypoint.converge(tmp_path, {}, lease)
    finally:
        lease._stop.set()
    assert fake.unlocked == ["01PREVIOUS"]
    assert fake.lock == {"id": "01APPLY1", "operation": "deployment"}


def test_a_lock_that_is_not_the_applys_is_not_released_after_apply(
    monkeypatch,
    tmp_path,
    dialect,  # noqa: F811
):
    fake = ApplyHook(dialect)
    fake.after_apply = lambda: setattr(
        fake, "lock", {"id": "01SOMEONE", "operation": "serve"}
    )
    monkeypatch.setattr(entrypoint, "run_omnigraph", fake)
    with pytest.raises(BootRefused, match="which this boot did not take"):
        entrypoint.converge(tmp_path, {}, entrypoint.NoPeers(may_unlock=True))
    assert fake.unlocked == []


class CountingKubernetes(FakeKubernetes):
    def __init__(self) -> None:
        super().__init__()
        self.conflicts = 0
        self.writes = 0

    def replace_lease(self, name: str, lease: dict) -> dict:
        try:
            replaced = super().replace_lease(name, lease)
        except KubernetesError:
            self.conflicts += 1
            raise
        self.writes += 1
        return replaced


def test_confirm_and_the_renew_thread_do_not_depose_each_other(monkeypatch):
    """Both write the Lease from the version the other last wrote."""
    monkeypatch.setattr(entrypoint, "LEASE_RENEW_INTERVAL_SECONDS", 0.0005)
    kube = CountingKubernetes()
    lease = entrypoint.UnlockLease(kube, LEASE, POD)
    lease.acquire()
    confirms = 0
    deadline = time.monotonic() + 0.5
    try:
        while time.monotonic() < deadline:
            lease.confirm(entrypoint.UNLOCK_BUDGET_SECONDS)
            confirms += 1
    finally:
        lease.hand_over()
    assert kube.conflicts == 0
    # More writes than the acquire, the confirms and the hand-over account
    # for: the renew thread really was writing in between.
    assert kube.writes > confirms + 2
    assert kube.spec["holderIdentity"] == lease.identity
