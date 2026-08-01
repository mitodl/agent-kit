"""witan-subclass-specific OmnigraphClient tests.

The generic base machinery (_find_binary lookup order, OCC conflict surfacing,
admission-cap backoff) is covered in packages/witan-core/tests/test_omnigraph.py.
Here we only assert witan's own subclass tail: the setup-hint in the
binary-not-found message. (apply_schema is exercised against a real store in
test_migrate.py.)
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from witan_core import omnigraph as og

from witan.graph import OmnigraphClient


def test_find_binary_message_names_witan_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="witan setup"):
        OmnigraphClient._find_binary()


def _client(monkeypatch, uri, **kwargs):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/omnigraph")
    return OmnigraphClient(uri, Path("/queries"), **kwargs)


def _capture_schema_apply(monkeypatch, client):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(og.subprocess, "run", fake_run)
    client.apply_schema("/schema/schema.pg")
    return captured["cmd"]


def test_apply_schema_local_passes_store_positionally(monkeypatch, tmp_path):
    store = str(tmp_path / "graph.omni")
    client = _client(monkeypatch, store)
    cmd = _capture_schema_apply(monkeypatch, client)
    # local `schema apply` takes the store as a trailing positional, not --store
    assert cmd[-1] == store
    assert "--store" not in cmd
    assert "--server" not in cmd


def test_apply_schema_remote_uses_server_and_graph(monkeypatch):
    client = _client(monkeypatch, "http://host:8080", graph_id="council")
    cmd = _capture_schema_apply(monkeypatch, client)
    assert cmd[cmd.index("--server") + 1] == "http://host:8080"
    assert cmd[cmd.index("--graph") + 1] == "council"
    assert "--store" not in cmd


# ── _ensure_graph: schema currency on an EXISTING store ────────────


def test_ensure_graph_reapplies_schema_when_it_changed(monkeypatch, tmp_path):
    """The bug this fixes: an existing store never saw additive schema changes,
    because _ensure_graph early-returned on store.exists()."""
    import witan.server as srv

    store = tmp_path / "graph.omni"
    store.mkdir()
    calls = []
    monkeypatch.setattr(srv, "_SCHEMA_FILE", tmp_path / "schema.pg")
    srv._SCHEMA_FILE.write_text("node Memory { slug: String }")
    monkeypatch.setattr(
        srv.OmnigraphClient, "_find_binary", staticmethod(lambda: "omnigraph")
    )
    monkeypatch.setattr(
        og.subprocess,
        "run",
        lambda cmd, **kw: (
            calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")
        ),
    )

    srv._ensure_graph(str(store))
    assert [c[:3] for c in calls] == [["omnigraph", "schema", "apply"]]

    # Unchanged schema: no second subprocess.
    srv._ensure_graph(str(store))
    assert len(calls) == 1

    srv._SCHEMA_FILE.write_text("node Memory { slug: String }\nnode Topic { x: I64 }")
    os.utime(srv._SCHEMA_FILE, (2_000_000_000, 2_000_000_000))
    srv._ensure_graph(str(store))
    assert len(calls) == 2


def test_ensure_graph_survives_a_failing_reapply(monkeypatch, tmp_path):
    """_ensure_graph runs at import time, so a failed re-apply against an
    existing, working store must not be able to brick `witan serve`."""
    import witan.server as srv

    store = tmp_path / "graph.omni"
    store.mkdir()
    monkeypatch.setattr(srv, "_SCHEMA_FILE", tmp_path / "schema.pg")
    srv._SCHEMA_FILE.write_text("node Memory { slug: String }")
    monkeypatch.setattr(
        srv.OmnigraphClient, "_find_binary", staticmethod(lambda: "omnigraph")
    )
    monkeypatch.setattr(
        og.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"),
    )

    srv._ensure_graph(str(store))  # must not raise


def test_ensure_graph_is_still_a_noop_for_remote_uris(monkeypatch):
    """A deployment's schema is managed by provisioning, and `schema apply`
    against a server takes --server/--graph, not the local positional form."""
    import witan.server as srv

    def fail(*args, **kwargs):
        raise AssertionError("should not shell out for a remote graph URI")

    monkeypatch.setattr(og.subprocess, "run", fail)
    monkeypatch.setattr(srv.OmnigraphClient, "_find_binary", staticmethod(fail))

    for uri in ("https://omnigraph.example/", "http://localhost:8080", "s3://b/g"):
        srv._ensure_graph(uri)
