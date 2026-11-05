"""The Omnigraph-If-Actor assertion against a real omnigraph-server.

The served test server runs unauthenticated, so it resolves no actor, and a
0.14 server refuses every request that asserts one (``require_expected_actor``
in omnigraph-server at v0.14.0). That is the mismatch case without having to
provision tokens: a client asserting an actor the credential does not resolve
to. A 0.13 server has no such check and ignores the header.

Needs ``omnigraph-server``; see the ``served`` fixture in conftest.py.
"""

from __future__ import annotations

import pytest
from testsupport.omnigraph_server import requires_omnigraph_server

from witan_core.omnigraph import HTTP_TRANSPORT_ENV_VAR
from witan_core.omnigraph_http import PooledTransport

from .conftest import SERVED_GRAPH, SERVED_REPO

pytestmark = requires_omnigraph_server


def _as_actor(monkeypatch, url, actor):
    from witan import config as cfg_mod
    from witan import graph as graph_mod
    from witan import server as srv

    client = graph_mod.OmnigraphClient(
        url, cfg_mod.load().queries_dir, graph_id=SERVED_GRAPH, actor=actor
    )
    monkeypatch.setattr(srv, "client", client)


def _titles(tools):
    return {t["title"] for t in tools.task_list(repo=SERVED_REPO)}


@pytest.mark.parametrize("transport", ["http", "cli"])
def test_a_write_asserting_another_actor_is_refused_before_it_lands(
    served, monkeypatch, transport
):
    tools, url = served
    if PooledTransport(url).contract() != "0.14":
        pytest.skip("only a 0.14 server checks the assertion")
    if transport == "cli":
        # The 0.14 CLI sends `--as` as the header; an anonymous loopback
        # server is the one plain-http target it accepts.
        monkeypatch.setenv(HTTP_TRANSPORT_ENV_VAR, "0")
    before = _titles(tools)
    _as_actor(monkeypatch, url, "act-pytest")
    # task_create's duplicate search is a read, and the server checks the
    # assertion on reads too, so without this the refusal comes from the
    # search and the write is never attempted.
    from witan import server as srv

    monkeypatch.setattr(srv, "_search_task_rows", lambda *_args: [])
    writes = []
    change_many = srv.client.change_many

    def recording_change_many(*args, **kwargs):
        writes.append(args)
        return change_many(*args, **kwargs)

    monkeypatch.setattr(srv.client, "change_many", recording_change_many)

    with pytest.raises(RuntimeError, match="does not belong to 'act-pytest'"):
        tools.task_create(title="misattributed", description="d", repo=SERVED_REPO)

    assert len(writes) == 1
    monkeypatch.delenv(HTTP_TRANSPORT_ENV_VAR, raising=False)
    _as_actor(monkeypatch, url, None)
    assert _titles(tools) == before


def test_a_013_server_ignores_the_assertion(served, monkeypatch):
    """So the header is sent whatever contract the server speaks, and the
    assertion applies once the data tier is on 0.14."""
    tools, url = served
    if PooledTransport(url).contract() != "0.13":
        pytest.skip("covers the 0.13 server only")
    _as_actor(monkeypatch, url, "act-pytest")

    tools.task_create(title="unchecked", description="d", repo=SERVED_REPO)

    assert "unchecked" in _titles(tools)
