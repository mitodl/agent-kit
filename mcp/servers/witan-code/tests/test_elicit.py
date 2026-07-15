"""Elicitation behavior for the witan-code tools (Opportunities 1-3).

Each tool must be strictly additive: a client without elicitation (no ``ctx``,
matching pre-elicitation automation) behaves exactly as before; an explicit
accept or decline changes the outcome. These tests drive the accept/decline
paths with fake contexts, modeled on ``mcp/servers/witan/tests/test_elicit.py``.
"""

import asyncio
import inspect

from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation

from .conftest import SAMPLE, requires_stack


class _AcceptCtx:
    def __init__(self, data):
        self._data = data

    async def elicit(self, message, response_type=None, **kwargs):
        return AcceptedElicitation(data=self._data)


class _DeclineCtx:
    async def elicit(self, message, response_type=None, **kwargs):
        return DeclinedElicitation()


def _fn(tool):
    """Unwrap + run a (possibly async) FastMCP tool directly, as the CLI does."""
    fn = getattr(tool, "fn", tool)
    if inspect.iscoroutinefunction(fn):

        def runner(*args, **kwargs):
            return asyncio.run(fn(*args, **kwargs))

        return runner
    return fn


# ── Opportunity 1: code_symbols_in_file elicits a repo when none detected ────


@requires_stack
def test_symbols_in_file_elicits_repo_when_undetected(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text(SAMPLE)

    repo = "https://github.com/test/elicited-repo"
    indexer.index_path(src, repo_override=repo, config=srv.cfg)

    # No WITAN_REPO, cwd is the same dir the file was indexed from (so
    # _file_id resolves the same relative path) but has no .git to detect from.
    monkeypatch.delenv("WITAN_REPO", raising=False)
    monkeypatch.chdir(src)
    srv._git_context.clear()

    path = str(src / "svc.py")

    accepted = _fn(srv.code_symbols_in_file)(path=path, ctx=_AcceptCtx(repo))
    names = {r["qualified_name"] for r in accepted}
    assert {"Service", "Service.run", "helper", "main"} <= names

    assert _fn(srv.code_symbols_in_file)(path=path, ctx=_DeclineCtx()) == []
    assert _fn(srv.code_symbols_in_file)(path=path) == []

    # An explicit repo short-circuits elicitation even with a declining ctx.
    explicit = _fn(srv.code_symbols_in_file)(path=path, repo=repo, ctx=_DeclineCtx())
    assert explicit


# ── Opportunity 2: offer to index now when a store is missing ────────────────


@requires_stack
def test_symbols_in_file_offers_reindex_when_store_missing(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    repo = "https://github.com/test/cg-reindex-file"
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text(SAMPLE)
    monkeypatch.chdir(src)
    srv._git_context.clear()

    path = str(src / "svc.py")

    # Declined / headless -> unchanged pre-elicitation behavior: [].
    assert _fn(srv.code_symbols_in_file)(path=path, ctx=_DeclineCtx()) == []
    assert _fn(srv.code_symbols_in_file)(path=path) == []

    # Accepted -> actually indexes and returns real symbols.
    result = _fn(srv.code_symbols_in_file)(path=path, ctx=_AcceptCtx(True))
    names = {r["qualified_name"] for r in result}
    assert {"Service", "helper", "main"} <= names

    # Store now exists -> no prompt needed even with a declining ctx.
    result2 = _fn(srv.code_symbols_in_file)(path=path, ctx=_DeclineCtx())
    assert {"Service", "helper", "main"} <= {r["qualified_name"] for r in result2}


@requires_stack
def test_find_references_offers_reindex_when_store_missing(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    repo = "https://github.com/test/cg-reindex-refs"
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text(SAMPLE)
    monkeypatch.chdir(src)
    srv._git_context.clear()

    symbol_id = f"{repo}#svc.py::helper"

    assert _fn(srv.code_find_references)(symbol_id, ctx=_DeclineCtx()) == []
    assert _fn(srv.code_find_references)(symbol_id) == []

    refs = _fn(srv.code_find_references)(symbol_id, ctx=_AcceptCtx(True))
    assert "Service.run" in {r["qualified_name"] for r in refs}

    # Store now exists -> no prompt needed even with a declining ctx.
    refs2 = _fn(srv.code_find_references)(symbol_id, ctx=_DeclineCtx())
    assert "Service.run" in {r["qualified_name"] for r in refs2}


@requires_stack
def test_callers_offers_reindex_when_store_missing(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    repo = "https://github.com/test/cg-reindex-callers"
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text(SAMPLE)
    monkeypatch.chdir(src)
    srv._git_context.clear()

    symbol_id = f"{repo}#svc.py::helper"

    assert _fn(srv.code_callers)(symbol_id, ctx=_DeclineCtx()) == []
    assert _fn(srv.code_callers)(symbol_id) == []

    callers = _fn(srv.code_callers)(symbol_id, ctx=_AcceptCtx(True))
    assert "Service.run" in {r["qualified_name"] for r in callers}


@requires_stack
def test_impact_offers_reindex_when_store_missing(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    repo = "https://github.com/test/cg-reindex-impact"
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text(SAMPLE)
    monkeypatch.chdir(src)
    srv._git_context.clear()

    symbol_id = f"{repo}#svc.py::helper"

    declined = _fn(srv.code_impact)(symbol_id, ctx=_DeclineCtx())
    assert declined == {"root": symbol_id, "impacted": [], "truncated": False}
    assert _fn(srv.code_impact)(symbol_id) == declined

    accepted = _fn(srv.code_impact)(symbol_id, ctx=_AcceptCtx(True))
    assert "Service.run" in {s["qualified_name"] for s in accepted["impacted"]}


@requires_stack
def test_interface_consumers_offers_bridge_reindex_when_missing(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    repo = "https://github.com/test/cg-bridge-consumers"
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    src = tmp_path / "src"
    src.mkdir()
    (src / "settings.py").write_text(
        'import os\nAPP_VALUE = os.getenv("WITAN_CODE_TEST_VAR")\n'
    )
    monkeypatch.chdir(src)
    srv._git_context.clear()

    kind, key = "env_var", "WITAN_CODE_TEST_VAR"

    assert _fn(srv.code_interface_consumers)(kind, key, ctx=_DeclineCtx()) == []
    assert _fn(srv.code_interface_consumers)(kind, key) == []

    rows = _fn(srv.code_interface_consumers)(kind, key, ctx=_AcceptCtx(True))
    assert rows and all(r["repo"] == repo for r in rows)

    # Bridge store now exists -> no prompt needed even with a declining ctx.
    rows2 = _fn(srv.code_interface_consumers)(kind, key, ctx=_DeclineCtx())
    assert rows2 and all(r["repo"] == repo for r in rows2)


@requires_stack
def test_cross_repo_impact_offers_bridge_reindex_when_missing(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    repo = "https://github.com/test/cg-bridge-impact"
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    src = tmp_path / "src"
    src.mkdir()
    (src / "settings.py").write_text(
        'import os\nAPP_VALUE = os.getenv("WITAN_CODE_TEST_VAR2")\n'
    )
    monkeypatch.chdir(src)
    srv._git_context.clear()

    symbol_id = f"{repo}#settings.py::<module>"
    empty = {"symbol_id": symbol_id, "bindings": [], "cross_repo": []}

    assert _fn(srv.code_cross_repo_impact)(symbol_id, ctx=_DeclineCtx()) == empty
    assert _fn(srv.code_cross_repo_impact)(symbol_id) == empty

    result = _fn(srv.code_cross_repo_impact)(symbol_id, ctx=_AcceptCtx(True))
    assert result["bindings"]


@requires_stack
def test_reindex_offer_never_crosses_repos(tmp_path, monkeypatch):
    """Safety rule: a symbol_id naming a repo other than the one we're sitting
    in must never trigger a reindex, even on an accepting ctx — code_reindex
    has no way to index anything but the current checkout."""
    from witan_code import config as cfg_mod
    from witan_code import server as srv
    from witan_code import store as store_module

    current_repo = "https://github.com/test/cg-current"
    other_repo = "https://github.com/test/cg-other-never-indexed"
    monkeypatch.setenv("WITAN_REPO", current_repo)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text(SAMPLE)
    monkeypatch.chdir(src)
    srv._git_context.clear()

    symbol_id = f"{other_repo}#svc.py::helper"

    assert _fn(srv.code_find_references)(symbol_id, ctx=_AcceptCtx(True)) == []
    assert _fn(srv.code_callers)(symbol_id, ctx=_AcceptCtx(True)) == []

    # Neither the other repo's store NOR the current repo's store were created
    # as a side effect — the mismatch short-circuits before any indexing.
    assert not store_module.store_for_repo(other_repo, srv.cfg).exists()
    assert not store_module.store_for_repo(current_repo, srv.cfg).exists()


@requires_stack
def test_reindex_offer_degrades_on_indexing_failure(tmp_path, monkeypatch):
    """An accepted offer whose indexing blows up must degrade to the same
    shaped-empty result as a decline, not propagate the exception."""
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    repo = "https://github.com/test/cg-reindex-failure"
    monkeypatch.setenv("WITAN_REPO", repo)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text(SAMPLE)
    monkeypatch.chdir(src)
    srv._git_context.clear()

    def _boom(*args, **kwargs):
        raise RuntimeError("indexing blew up")

    monkeypatch.setattr(indexer, "index_path", _boom)

    symbol_id = f"{repo}#svc.py::helper"
    assert _fn(srv.code_find_references)(symbol_id, ctx=_AcceptCtx(True)) == []
    assert _fn(srv.code_interface_consumers)("env_var", "X", ctx=_AcceptCtx(True)) == []


# ── Opportunity 3: code_find_definition disambiguates multi-repo matches ─────

RA = "https://github.com/test/def-repo-a"
RB = "https://github.com/test/def-repo-b"

PY_WIDGET_A = "def widget():\n    return 1\n"
PY_WIDGET_B = "def widget():\n    return 2\n"
PY_ONLY_A = "def onlya():\n    return 1\n"


@requires_stack
def test_find_definition_multi_repo_disambiguation(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    a = tmp_path / "a"
    a.mkdir()
    (a / "m.py").write_text(PY_WIDGET_A)
    (a / "solo.py").write_text(PY_ONLY_A)
    b = tmp_path / "b"
    b.mkdir()
    (b / "m.py").write_text(PY_WIDGET_B)
    indexer.index_path(a, repo_override=RA, config=srv.cfg)
    indexer.index_path(b, repo_override=RB, config=srv.cfg)

    monkeypatch.setenv("WITAN_REPO", "")  # detect() -> None -> fan out
    srv._git_context.clear()

    # Valid repo answer narrows to just that repo's matches.
    narrowed = _fn(srv.code_find_definition)("widget", ctx=_AcceptCtx(RA))
    assert {d["repo"] for d in narrowed} == {RA}

    # An answer that names no candidate repo -> fall back to every match.
    invalid = _fn(srv.code_find_definition)("widget", ctx=_AcceptCtx("not-a-repo"))
    assert {d["repo"] for d in invalid} == {RA, RB}

    # Decline / headless -> every match (today's behavior, unchanged).
    declined = _fn(srv.code_find_definition)("widget", ctx=_DeclineCtx())
    assert {d["repo"] for d in declined} == {RA, RB}
    headless = _fn(srv.code_find_definition)("widget")
    assert {d["repo"] for d in headless} == {RA, RB}

    # An explicit repo never prompts, even with a declining ctx.
    scoped = _fn(srv.code_find_definition)("widget", repo=RA, ctx=_DeclineCtx())
    assert {d["repo"] for d in scoped} == {RA}

    # A name matching in only one repo never prompts: behavior is identical
    # regardless of ctx (an accepted-but-wrong-repo answer would otherwise
    # narrow the result to nothing).
    r1 = _fn(srv.code_find_definition)("onlya", ctx=_AcceptCtx(RB))
    r2 = _fn(srv.code_find_definition)("onlya", ctx=_DeclineCtx())
    r3 = _fn(srv.code_find_definition)("onlya")
    assert r1 == r2 == r3
    assert {d["repo"] for d in r1} == {RA}


# ── choose_repo unit tests (no server/omnigraph needed) ──
# The confirm/text primitives are covered in witan-core's own test suite
# (packages/witan-core/tests/test_elicit.py); only choose_repo is witan-code's.


def test_choose_repo_exact_match_case_insensitive_and_stripped():
    from witan_code import elicit

    repos = ["https://github.com/x/a", "https://github.com/x/b"]
    chosen = asyncio.run(
        elicit.choose_repo(_AcceptCtx("  HTTPS://GITHUB.COM/X/A  "), "q?", repos)
    )
    assert chosen == "https://github.com/x/a"


def test_choose_repo_no_match_or_declined_returns_none():
    from witan_code import elicit

    repos = ["https://github.com/x/a", "https://github.com/x/b"]
    assert asyncio.run(elicit.choose_repo(_AcceptCtx("nope"), "q?", repos)) is None
    assert asyncio.run(elicit.choose_repo(_DeclineCtx(), "q?", repos)) is None
    assert asyncio.run(elicit.choose_repo(None, "q?", repos)) is None
