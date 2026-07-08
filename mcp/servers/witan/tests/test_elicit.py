"""Elicitation behavior for the four interactive tools (C1–C4).

Each tool must be strictly additive: a client without elicitation (the default
_NoElicitCtx the harness injects) behaves exactly as before; an explicit accept
or decline changes the outcome. These tests drive the accept/decline paths with
fake contexts, and lean on the harness's no-elicit ctx for the fallback path.
"""

from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation

from .conftest import requires_omnigraph


class _AcceptCtx:
    def __init__(self, data):
        self._data = data

    async def elicit(self, message, response_type=None, **kwargs):
        return AcceptedElicitation(data=self._data)


class _DeclineCtx:
    async def elicit(self, message, response_type=None, **kwargs):
        return DeclinedElicitation()


# ── C1: task_claim force-steal confirm ───────────────────────────────────────


@requires_omnigraph
def test_claim_steal_confirmed(server):
    t = server.task_create(title="hot", description="x")
    server.task_claim(t["slug"], assignee="agentA")
    # agentB accepts the steal prompt → takes the live claim without force=True
    r = server.task_claim(t["slug"], assignee="agentB", ctx=_AcceptCtx(True))
    assert r["claimed"] is True and r["stole"] is True
    assert server.task_get(t["slug"])["assignee"] == "agentB"


@requires_omnigraph
def test_claim_steal_declined_leaves_holder(server):
    t = server.task_create(title="hot2", description="x")
    server.task_claim(t["slug"], assignee="agentA")
    r = server.task_claim(t["slug"], assignee="agentB", ctx=_DeclineCtx())
    assert r["claimed"] is False and r["reason"] == "held"
    assert server.task_get(t["slug"])["assignee"] == "agentA"


@requires_omnigraph
def test_claim_steal_unsupported_leaves_holder(server):
    # Default harness ctx can't elicit → historical behavior: refuse (no steal).
    t = server.task_create(title="hot3", description="x")
    server.task_claim(t["slug"], assignee="agentA")
    r = server.task_claim(t["slug"], assignee="agentB")
    assert r["claimed"] is False and r["reason"] == "held"


# ── C2: memory_link supersedes confirm ───────────────────────────────────────


def _two_memories(server):
    a = server.memory_store(kind="lesson", title="new", content="newer")
    b = server.memory_store(kind="lesson", title="old", content="older")
    return a["slug"], b["slug"]


@requires_omnigraph
def test_supersede_confirmed_links(server):
    new, old = _two_memories(server)
    r = server.memory_link(new, old, kind="supersedes", ctx=_AcceptCtx(True))
    assert r["linked"] is True


@requires_omnigraph
def test_supersede_declined_does_not_link(server):
    new, old = _two_memories(server)
    r = server.memory_link(new, old, kind="supersedes", ctx=_DeclineCtx())
    assert r["linked"] is False and r["reason"] == "declined"


@requires_omnigraph
def test_supersede_unsupported_proceeds(server):
    # Default harness ctx can't elicit → additive: keep the historical behavior
    # (link it) rather than silently dropping a supersede in headless automation.
    new, old = _two_memories(server)
    r = server.memory_link(new, old, kind="supersedes")
    assert r["linked"] is True


@requires_omnigraph
def test_non_supersede_link_never_prompts(server):
    # A declining ctx must not affect a non-supersede kind (no prompt for it).
    new, old = _two_memories(server)
    r = server.memory_link(new, old, kind="related_to", ctx=_DeclineCtx())
    assert r["linked"] is True


# ── C3: workflow_project_complete thin-outcome narrative ─────────────────────


@requires_omnigraph
def test_complete_thin_outcome_uses_elicited(server):
    p = server.workflow_project_create(title="c3", description="d")
    full = "Delivered the full widget pipeline with tests and docs."
    server.workflow_project_complete(p["slug"], outcome="wip", ctx=_AcceptCtx(full))
    trace = server.client.read("read.gq", "get_trace", {"slug": f"wt-{p['slug']}"})
    assert trace[0]["outcome"] == full


@requires_omnigraph
def test_complete_thin_outcome_declined_keeps_given(server):
    p = server.workflow_project_create(title="c3b", description="d")
    server.workflow_project_complete(p["slug"], outcome="wip", ctx=_DeclineCtx())
    trace = server.client.read("read.gq", "get_trace", {"slug": f"wt-{p['slug']}"})
    assert trace[0]["outcome"] == "wip"


@requires_omnigraph
def test_complete_full_outcome_never_prompts(server):
    # A long outcome is above the thin threshold → no elicit even with a decline.
    p = server.workflow_project_create(title="c3c", description="d")
    good = "A thorough narrative of everything that was delivered here, at length."
    server.workflow_project_complete(p["slug"], outcome=good, ctx=_DeclineCtx())
    trace = server.client.read("read.gq", "get_trace", {"slug": f"wt-{p['slug']}"})
    assert trace[0]["outcome"] == good


# ── C4: workflow_project_advance unusual-transition confirm ───────────────────


@requires_omnigraph
def test_advance_backward_declined_does_not_change(server):
    p = server.workflow_project_create(
        title="c4", description="d", phase="implementation"
    )
    r = server.workflow_project_advance(p["slug"], phase="discovery", ctx=_DeclineCtx())
    assert r["advanced"] is False
    assert server.workflow_project_get(p["slug"])["phase"] == "implementation"


@requires_omnigraph
def test_advance_backward_confirmed_changes(server):
    p = server.workflow_project_create(
        title="c4b", description="d", phase="implementation"
    )
    r = server.workflow_project_advance(
        p["slug"], phase="discovery", ctx=_AcceptCtx(True)
    )
    assert "advisory" in r
    assert server.workflow_project_get(p["slug"])["phase"] == "discovery"


@requires_omnigraph
def test_advance_normal_step_never_prompts(server):
    # A normal forward step has no advisory → declining ctx is irrelevant.
    p = server.workflow_project_create(title="c4c", description="d", phase="discovery")
    server.workflow_project_advance(p["slug"], phase="spec", ctx=_DeclineCtx())
    assert server.workflow_project_get(p["slug"])["phase"] == "spec"


# ── elicit helper contract (no omnigraph needed) ─────────────────────────────


import asyncio  # noqa: E402

from witan import elicit  # noqa: E402


class _RaiseCtx:
    async def elicit(self, *args, **kwargs):
        raise RuntimeError("unsupported")


def test_confirm_no_ctx_or_error_returns_default():
    assert (
        asyncio.run(elicit.confirm(None, "q?", default_when_unsupported=True)) is True
    )
    assert (
        asyncio.run(elicit.confirm(None, "q?", default_when_unsupported=False)) is False
    )
    assert (
        asyncio.run(elicit.confirm(_RaiseCtx(), "q?", default_when_unsupported=True))
        is True
    )


def test_confirm_accept_and_decline():
    assert (
        asyncio.run(
            elicit.confirm(_AcceptCtx(True), "q?", default_when_unsupported=False)
        )
        is True
    )
    # accepting with a False value is still a "no"
    assert (
        asyncio.run(
            elicit.confirm(_AcceptCtx(False), "q?", default_when_unsupported=True)
        )
        is False
    )
    assert (
        asyncio.run(elicit.confirm(_DeclineCtx(), "q?", default_when_unsupported=True))
        is False
    )


def test_text_no_ctx_error_or_empty_returns_default():
    assert asyncio.run(elicit.text(None, "q?", default="d")) == "d"
    assert asyncio.run(elicit.text(_RaiseCtx(), "q?", default="d")) == "d"
    assert asyncio.run(elicit.text(_AcceptCtx(""), "q?", default="d")) == "d"
    assert asyncio.run(elicit.text(_AcceptCtx("real"), "q?", default="d")) == "real"
