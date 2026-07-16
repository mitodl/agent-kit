"""witan-subclass-specific OmnigraphClient tests.

The generic base machinery (_find_binary lookup order, OCC conflict surfacing,
admission-cap backoff) is covered in packages/witan-core/tests/test_omnigraph.py.
Here we only assert witan's own subclass tail: the setup-hint in the
binary-not-found message. (apply_schema is exercised against a real store in
test_migrate.py.)
"""

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
