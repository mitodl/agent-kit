"""A real, throwaway one-graph ``omnigraph-server`` for a served test.

``omnigraph-server`` ships in the omnigraph release tarball but ``witan
setup`` installs only the CLI; ``bin/install_omnigraph_server.py`` fetches it.
Set ``OMNIGRAPH_SERVER_BIN``, or put it on PATH; otherwise a test marked
:data:`requires_omnigraph_server` skips, unless ``WITAN_REQUIRE_OMNIGRAPH`` is
set, as it is in CI.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

SERVER_BIN = os.environ.get("OMNIGRAPH_SERVER_BIN") or shutil.which("omnigraph-server")
_REQUIRED = os.environ.get("WITAN_REQUIRE_OMNIGRAPH", "") not in ("", "0", "false")
requires_omnigraph_server = pytest.mark.skipif(
    SERVER_BIN is None and not _REQUIRED,
    reason="omnigraph-server not found (set OMNIGRAPH_SERVER_BIN)",
)
STARTUP_TIMEOUT_SECONDS = 20


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _cli() -> str:
    sibling = Path(SERVER_BIN).with_name("omnigraph")
    return str(sibling) if sibling.exists() else "omnigraph"


@contextmanager
def serve(workdir: Path, schema: Path, graph_id: str) -> Iterator[str]:
    """Apply a one-graph cluster under ``workdir`` and serve it unauthenticated.

    :param workdir: an empty directory for the cluster and the server log.
    :param schema: the ``.pg`` schema the graph is applied with.
    :param graph_id: the id the graph is served under.
    :returns: the server's base URL, once ``/readyz`` answers 200. ``/healthz``
        answers while the graph is still loading, and a graph-scoped request in
        that window gets 503 ``graph_unavailable``, which a test's own raw
        request does not retry.
    :rtype: str
    """
    cluster = workdir / "cluster"
    cluster.mkdir()
    shutil.copy(schema, cluster / "schema.pg")
    (cluster / "cluster.yaml").write_text(
        f"version: 1\ngraphs:\n  {graph_id}:\n    schema: schema.pg\n"
    )
    applied = subprocess.run(
        [_cli(), "cluster", "apply", "--config", str(cluster)],
        check=True,
        capture_output=True,
        text=True,
    )
    # A direct apply leaves its admission lock behind for the operator to
    # release, and the server refuses to boot while it is held.
    lock = next(
        line.split()[2].rstrip(";")
        for line in applied.stderr.splitlines() + applied.stdout.splitlines()
        if line.startswith("Admission lock:")
    )
    subprocess.run(
        [_cli(), "cluster", "force-unlock", "--config", str(cluster), lock],
        check=True,
        capture_output=True,
    )
    port = _free_port()
    log_path = workdir / "server.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [
                SERVER_BIN,
                "--cluster",
                str(cluster),
                "--unauthenticated",
                "--bind",
                f"127.0.0.1:{port}",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
            while True:
                try:
                    urllib.request.urlopen(f"{url}/readyz", timeout=1).close()
                    break
                except OSError:
                    assert proc.poll() is None, log_path.read_text()
                    assert time.monotonic() < deadline, (
                        "omnigraph-server never became ready"
                    )
                    time.sleep(0.1)
            yield url
        finally:
            proc.terminate()
            proc.wait(10)
