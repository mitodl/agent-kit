"""Cross-repo symbol resolution: scope by repo, route by symbol id, fan out."""

import asyncio
import inspect

from .conftest import requires_stack

PY_A = """\
def alpha():
    return shared()


def shared():
    return 1
"""

PY_B = """\
def beta():
    return shared()


def shared():
    return 2
"""

RA = "https://github.com/test/repo-a"
RB = "https://github.com/test/repo-b"


def _fn(tool):
    """Unwrap + run a (possibly async) FastMCP tool directly, as the CLI does."""
    fn = getattr(tool, "fn", tool)
    if inspect.iscoroutinefunction(fn):

        def runner(*args, **kwargs):
            return asyncio.run(fn(*args, **kwargs))

        return runner
    return fn


@requires_stack
def test_repo_scope_route_and_fanout(tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    a = tmp_path / "a"
    a.mkdir()
    (a / "m.py").write_text(PY_A)
    b = tmp_path / "b"
    b.mkdir()
    (b / "m.py").write_text(PY_B)
    indexer.index_path(a, repo_override=RA, config=srv.cfg)
    indexer.index_path(b, repo_override=RB, config=srv.cfg)

    # repo-scoped: only the named repo answers.
    da = _fn(srv.code_find_definition)("alpha", repo=RA)
    assert da and all(d["repo"] == RA for d in da)
    assert _fn(srv.code_find_definition)("alpha", repo=RB) == []

    # cross-repo fan-out: with no repo and not inside an indexed repo, `shared`
    # comes back from BOTH stores, tagged by origin.
    monkeypatch.setenv("WITAN_REPO", "")  # detect() -> None -> fan out
    srv._clients.clear()
    shared = _fn(srv.code_find_definition)("shared")
    assert {RA, RB} <= {d["repo"] for d in shared}

    # symbol-id routing: callers of repo-a's `shared` resolve in repo-a's store.
    a_shared = _fn(srv.code_find_definition)("shared", repo=RA)[0]
    callers = _fn(srv.code_callers)(a_shared["symbol_id"])
    assert any(c["qualified_name"] == "alpha" for c in callers)
    assert all(c["repo"] == RA for c in callers)
