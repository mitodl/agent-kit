"""``memory_stale_refs``: memories whose symbol_refs no longer resolve.

witan-code is not installed in this package's test env, so a stand-in code
server answers ``code_indexed_repos``, ``code_symbols_in_file`` and
``code_find_definition``. The last two are ``async`` like the real ones, which
is what caught ``recall(memory=...)``'s symbols storing an un-awaited coroutine
as the definition.
"""

from __future__ import annotations

import json
import types

import pytest

from .conftest import requires_omnigraph

INDEXED = "https://github.com/test/repo"
UNINDEXED = "https://github.com/test/elsewhere"
LIVE = f"{INDEXED}#app/svc.py::Service.run"
RENAMED_TO = f"{INDEXED}#app/runner.py::Service.run"
DELETED = f"{INDEXED}#app/old.py::gone"


def _fake_code(symbols: set[str], indexed: dict[str, int | None], name_cap: int = 50):
    """A witan-code stand-in serving ``symbols`` for the repos in ``indexed``.

    :param symbols: Symbol ids the graph defines.
    :param indexed: Repo URI to file count; ``None`` marks an unreadable store.
    :param name_cap: Rows a name lookup returns, like the real query's
        ``limit 50``.
    """

    async def code_find_definition(name, repo=None, branch=None, ctx=None):
        return [
            {"symbol_id": s, "repo": s.split("#", 1)[0]}
            for s in sorted(symbols)
            if s.startswith(f"{repo}#") and s.rsplit("::", 1)[1] == name
        ][:name_cap]

    async def code_symbols_in_file(path, repo=None, ctx=None):
        return [
            {"symbol_id": s}
            for s in sorted(symbols)
            if s.startswith(f"{repo}#{path}::")
        ]

    def code_indexed_repos():
        return [
            {"repo": repo, "files": files, "unreadable": None if files else "boom"}
            for repo, files in indexed.items()
        ]

    return types.SimpleNamespace(
        code_find_definition=code_find_definition,
        code_indexed_repos=code_indexed_repos,
        code_symbols_in_file=code_symbols_in_file,
    )


@pytest.fixture
def code(monkeypatch):
    from witan import server as srv

    def install(fake):
        monkeypatch.setattr(srv._code_server, "_cached", fake, raising=False)

    return install


def _lesson(server, title, refs, repo=INDEXED):
    return server.memory_store(
        kind="lesson",
        title=title,
        content=f"about {title}",
        severity="info",
        repo=repo,
        symbol_refs=refs,
    )["slug"]


def _by_slug(result):
    return {m["slug"]: m for m in result["memories"]}


@requires_omnigraph
def test_deleted_symbol_is_stale_and_live_one_is_not_reported(server, code):
    code(_fake_code({LIVE}, {INDEXED: 10}))
    live = _lesson(server, "live", [LIVE])
    stale = _lesson(server, "stale", [DELETED])

    out = server.memory_stale_refs(repo=INDEXED)

    flagged = _by_slug(out)
    assert live not in flagged
    assert flagged[stale]["status"] == "stale"
    assert flagged[stale]["refs"] == [
        {"symbol_ref": DELETED, "status": "missing", "candidates": []}
    ]
    assert out["checked"] == 2
    assert out["counts"] == {"live": 1, "missing": 1, "unknown": 0}


@requires_omnigraph
def test_a_live_symbol_whose_name_repeats_past_the_lookup_cap_is_live(server, code):
    # Every file has a `<module>` symbol, and a name lookup returns only the
    # first 50 of them, so liveness has to come from the ref's own file.
    modules = {f"{INDEXED}#pkg/m{i:02}.py::<module>" for i in range(3)}
    target = f"{INDEXED}#pkg/m02.py::<module>"
    code(_fake_code(modules, {INDEXED: 3}, name_cap=1))
    slug = _lesson(server, "module", [target])

    out = server.memory_stale_refs(repo=INDEXED)

    assert slug not in _by_slug(out)
    assert out["counts"]["live"] == 1


@requires_omnigraph
def test_a_symbol_the_file_read_misses_is_live_when_the_name_lookup_finds_it(
    server, code
):
    # witan-code's file read resolves a relative path against the cwd when it
    # exists there, so it can read the wrong file; the id lookup is the backstop.
    fake = _fake_code({LIVE}, {INDEXED: 10})

    async def wrong_file(path, repo=None, ctx=None):
        return []

    fake.code_symbols_in_file = wrong_file
    code(fake)
    slug = _lesson(server, "cwd collision", [LIVE])

    assert slug not in _by_slug(server.memory_stale_refs(repo=INDEXED))


@requires_omnigraph
def test_missing_symbol_lists_same_named_candidates(server, code):
    # A move to another file is the common "stale" case: the memory may still be
    # true, so the report names where the symbol went rather than editing it.
    moved = f"{INDEXED}#app/old_svc.py::Service.run"
    code(_fake_code({RENAMED_TO}, {INDEXED: 10}))
    slug = _lesson(server, "moved", [moved])

    (ref,) = _by_slug(server.memory_stale_refs(repo=INDEXED))[slug]["refs"]

    assert ref["status"] == "missing"
    assert ref["candidates"] == [RENAMED_TO]


@requires_omnigraph
def test_unindexed_repo_is_unknown_not_stale(server, code):
    code(_fake_code({LIVE}, {INDEXED: 10}))
    slug = _lesson(server, "elsewhere", [f"{UNINDEXED}#a.py::f"], repo=UNINDEXED)

    out = server.memory_stale_refs(repo=UNINDEXED)

    memory = _by_slug(out)[slug]
    assert memory["status"] == "unknown"
    assert memory["refs"][0]["status"] == "unknown"
    assert "not indexed" in memory["refs"][0]["reason"]
    assert out["counts"]["missing"] == 0


@requires_omnigraph
def test_unreadable_store_is_unknown(server, code):
    code(_fake_code(set(), {INDEXED: None}))
    slug = _lesson(server, "unreadable", [DELETED])

    (ref,) = _by_slug(server.memory_stale_refs(repo=INDEXED))[slug]["refs"]

    assert ref["status"] == "unknown"
    assert "unreadable" in ref["reason"]


@requires_omnigraph
def test_without_witan_code_every_ref_is_unknown(server, code):
    code(None)
    slug = _lesson(server, "no code graph", [LIVE, DELETED])

    out = server.memory_stale_refs(repo=INDEXED)

    assert {r["status"] for r in _by_slug(out)[slug]["refs"]} == {"unknown"}
    assert out["counts"] == {"live": 0, "missing": 0, "unknown": 2}


@requires_omnigraph
def test_unreachable_code_graph_is_unknown(server, code):
    def broken():
        raise ConnectionError("cluster down")

    fake = _fake_code({LIVE}, {INDEXED: 10})
    fake.code_indexed_repos = broken
    code(fake)
    slug = _lesson(server, "unreachable", [LIVE])

    (ref,) = _by_slug(server.memory_stale_refs(repo=INDEXED))[slug]["refs"]

    assert ref == {
        "symbol_ref": LIVE,
        "status": "unknown",
        "reason": "code graph unreachable",
    }


@requires_omnigraph
def test_a_memory_with_one_missing_ref_is_stale_and_keeps_its_live_ref(server, code):
    code(_fake_code({LIVE}, {INDEXED: 10}))
    slug = _lesson(server, "mixed", [LIVE, DELETED, "not-a-symbol-id"])

    memory = _by_slug(server.memory_stale_refs(repo=INDEXED))[slug]

    assert memory["status"] == "stale"
    assert [(r["symbol_ref"], r["status"]) for r in memory["refs"]] == [
        (LIVE, "live"),
        (DELETED, "missing"),
        ("not-a-symbol-id", "unknown"),
    ]


@pytest.mark.parametrize(
    "ref",
    [
        # No repo prefix: resolving it against the cwd's repo (WITAN_REPO here,
        # which is INDEXED) would judge it against whatever repo the caller
        # happens to be in.
        "app/old.py::gone",
        "#app/old.py::gone",
        f"{INDEXED}#::gone",
        f"{INDEXED}#app/old.py::",
    ],
)
@requires_omnigraph
def test_a_ref_without_repo_path_and_name_is_unknown(server, code, ref):
    code(_fake_code({LIVE}, {INDEXED: 10}))
    slug = _lesson(server, "malformed", [ref])

    (resolved,) = _by_slug(server.memory_stale_refs(repo=INDEXED))[slug]["refs"]

    assert resolved == {
        "symbol_ref": ref,
        "status": "unknown",
        "reason": "not a repo#path::Name id",
    }


@requires_omnigraph
def test_memories_without_refs_and_superseded_ones_are_not_checked(server, code):
    code(_fake_code(set(), {INDEXED: 10}))
    server.memory_store(kind="project_fact", title="plain", content="no refs")
    old = _lesson(server, "old", [DELETED])
    new = _lesson(server, "new", [LIVE])
    server.memory_link(new, old, "supersedes")

    out = server.memory_stale_refs(repo=INDEXED)

    assert out["checked"] == 1
    assert old not in _by_slug(out)


@requires_omnigraph
def test_all_repos_checks_every_repo(server, code):
    code(_fake_code({LIVE}, {INDEXED: 10}))
    here = _lesson(server, "here", [DELETED])
    there = _lesson(server, "there", [f"{UNINDEXED}#a.py::f"], repo=UNINDEXED)

    flagged = _by_slug(server.memory_stale_refs(all_repos=True))

    assert {here, there} <= set(flagged)


@requires_omnigraph
def test_recall_memory_symbols_await_an_async_definition_lookup(server, code):
    code(_fake_code({LIVE}, {INDEXED: 10}))
    slug = _lesson(server, "symbols", [LIVE])

    (symbol,) = server.recall(memory=slug, hops=0)["memory"]["symbols"]

    assert symbol["definition"] == [{"symbol_id": LIVE, "repo": INDEXED}]


@requires_omnigraph
def test_stale_refs_cli_lists_the_flagged_refs(server, code, monkeypatch, capsys):
    from witan.cli import _common
    from witan.cli.memory import stale_refs
    from witan.cli.output import set_output_format

    monkeypatch.setattr(_common, "_server", server)
    code(_fake_code({LIVE}, {INDEXED: 10}))
    live = _lesson(server, "live", [LIVE])
    stale = _lesson(server, "stale", [DELETED])
    set_output_format("json")
    try:
        stale_refs(repo=INDEXED)
    finally:
        set_output_format("txt")

    payload = json.loads(capsys.readouterr().out)
    assert [m["slug"] for m in payload["rows"]] == [stale]
    assert live not in {m["slug"] for m in payload["rows"]}


@requires_omnigraph
def test_stale_refs_cli_all_repos_checks_every_repo(server, code, monkeypatch, capsys):
    from witan.cli import _common
    from witan.cli.memory import stale_refs
    from witan.cli.output import set_output_format

    monkeypatch.setattr(_common, "_server", server)
    code(_fake_code({LIVE}, {INDEXED: 10}))
    here = _lesson(server, "here", [DELETED])
    there = _lesson(server, "there", [f"{UNINDEXED}#a.py::f"], repo=UNINDEXED)
    set_output_format("json")
    try:
        stale_refs(all_repos=True)
    finally:
        set_output_format("txt")

    payload = json.loads(capsys.readouterr().out)
    assert {here, there} <= {m["slug"] for m in payload["rows"]}
