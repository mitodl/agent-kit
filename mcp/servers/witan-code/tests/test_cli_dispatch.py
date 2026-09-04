"""Every read command goes through ``_srv()``, never straight to a store.

That indirection is what gives the CLI a remote mode (ADR 0005, path a): if a
command reaches for ``OmnigraphClient`` itself it silently loses it, and only
works against the local ``~/.local/share/witan/code`` stores. These tests stub
``_srv()`` and make constructing an ``OmnigraphClient`` an error, so a
regression here fails loudly instead of quietly going local-only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from witan_code import cli as cli_module


@pytest.fixture(autouse=True)
def _no_direct_store_access(monkeypatch):
    from witan_code import graph

    def _boom(*_args, **_kwargs):
        raise AssertionError(
            "read command built an OmnigraphClient directly — it must dispatch "
            "through _srv() so remote mode keeps working"
        )

    monkeypatch.setattr(graph, "OmnigraphClient", _boom)


def _stub(**tools):
    """A stand-in for the tool provider that records the calls it receives."""
    calls: list[tuple[str, dict]] = []

    def _record(name, result):
        def _call(**kwargs):
            calls.append((name, kwargs))
            return result

        return _call

    srv = SimpleNamespace(**{n: _record(n, r) for n, r in tools.items()})
    srv.calls = calls  # type: ignore[attr-defined]
    return srv


def test_symbols_dispatches_to_code_repo_symbols(monkeypatch, capsys):
    srv = _stub(
        code_repo_symbols=[
            {
                "role": "exported",
                "symbol": "env:API_URL",
                "kind": "env_var",
                "n_refs": 2,
                "confidence": 0.9,
                "file": "app/settings.py",
                "line": 12,
            }
        ]
    )
    monkeypatch.setattr(cli_module, "_srv", lambda: srv)

    cli_module.symbols(repo="https://github.com/test/repo", role="exported")

    assert srv.calls == [
        (
            "code_repo_symbols",
            {
                "repo": "https://github.com/test/repo",
                "role": "exported",
                "scheme": None,
            },
        )
    ]
    out = capsys.readouterr().out
    assert "API_URL" in out


def test_symbols_resolves_the_repo_client_side(monkeypatch):
    """The deployment has no checkout, so detection has to happen here."""
    srv = _stub(code_repo_symbols=[])
    monkeypatch.setattr(cli_module, "_srv", lambda: srv)
    monkeypatch.setattr(
        "witan_code.repo.detect", lambda *a, **kw: "https://github.com/test/detected"
    )

    cli_module.symbols()

    assert srv.calls[0][1]["repo"] == "https://github.com/test/detected"


def test_stitch_dispatches_to_precise_edges(monkeypatch, capsys):
    srv = _stub(
        code_precise_edges=[
            {
                "consumer_repo": "https://github.com/test/a",
                "provider_repo": "https://github.com/test/b",
                "kind": "package",
                "match_count": 2,
                "preferred": True,
                "ambiguous_version": False,
            }
        ]
    )
    monkeypatch.setattr(cli_module, "_srv", lambda: srv)

    cli_module.stitch()

    assert srv.calls == [("code_precise_edges", {"repo": None})]
    assert "test/a" in capsys.readouterr().out


def test_stitch_unresolved_dispatches_to_unresolved_symbols(monkeypatch, capsys):
    srv = _stub(
        code_unresolved_symbols=[
            {
                "repo": "https://github.com/test/a",
                "symbol": "pkg:npm/left-pad",
                "kind": "package",
                "n_refs": 1,
            }
        ]
    )
    monkeypatch.setattr(cli_module, "_srv", lambda: srv)

    cli_module.stitch(repo="https://github.com/test/a", unresolved=True)

    assert srv.calls == [
        ("code_unresolved_symbols", {"repo": "https://github.com/test/a"})
    ]
    assert "left-pad" in capsys.readouterr().out


def test_deps_dispatches_to_repo_dependencies(monkeypatch, capsys):
    srv = _stub(
        code_repo_dependencies={
            "repos": ["https://github.com/test/a", "https://github.com/test/b"],
            "edges": [
                {
                    "consumer": "https://github.com/test/a",
                    "provider": "https://github.com/test/b",
                    "weight": 3,
                    "kinds": {"env_var": 3},
                    "contracts": [
                        {"kind": "env_var", "key": "API_URL", "confidence": 1.0}
                    ],
                }
            ],
        }
    )
    monkeypatch.setattr(cli_module, "_srv", lambda: srv)

    cli_module.deps(kind="env_var")

    assert srv.calls == [
        (
            "code_repo_dependencies",
            {"kind": "env_var", "repo": None, "min_precision": "heuristic"},
        )
    ]
    out = capsys.readouterr().out
    assert "test/a" in out and "test/b" in out


def test_deps_renders_html_from_the_returned_graph(monkeypatch, tmp_path):
    srv = _stub(
        code_repo_dependencies={
            "repos": ["https://github.com/test/a", "https://github.com/test/b"],
            "edges": [
                {
                    "consumer": "https://github.com/test/a",
                    "provider": "https://github.com/test/b",
                    "weight": 1,
                    "kinds": {"package": 1},
                    "contracts": [{"kind": "package", "key": "left-pad"}],
                }
            ],
        }
    )
    monkeypatch.setattr(cli_module, "_srv", lambda: srv)
    out = tmp_path / "graph.html"

    cli_module.deps(html=out)

    html = out.read_text()
    assert "test/a" in html and "left-pad" in html


def test_repos_dispatches_to_indexed_repos(monkeypatch, capsys):
    srv = _stub(
        code_indexed_repos=[
            {
                "repo": "https://github.com/test/a",
                "files": 12,
                "bytes": 2048,
                "last_indexed": 1_800_000_000.0,
            }
        ]
    )
    monkeypatch.setattr(cli_module, "_srv", lambda: srv)

    cli_module.repos()

    assert srv.calls == [("code_indexed_repos", {})]
    assert "test/a" in capsys.readouterr().out


def test_repos_shows_a_question_mark_for_an_unreadable_store(monkeypatch, capsys):
    srv = _stub(
        code_indexed_repos=[
            {
                "repo": "https://github.com/test/a",
                "files": None,
                "bytes": 0,
                "last_indexed": 1_800_000_000.0,
            }
        ]
    )
    monkeypatch.setattr(cli_module, "_srv", lambda: srv)

    cli_module.repos()

    assert "?" in capsys.readouterr().out


def test_branches_dispatches_to_indexed_branches(monkeypatch, capsys):
    srv = _stub(
        code_indexed_branches=[
            {
                "repo": "https://github.com/test/a",
                "views": [
                    {
                        "view": "act-alice/feature-x",
                        "branch": "feature-x",
                        "actor": "act-alice",
                    }
                ],
            }
        ]
    )
    monkeypatch.setattr(cli_module, "_srv", lambda: srv)

    cli_module.branches()

    assert srv.calls == [("code_indexed_branches", {"branch": None})]
    out = capsys.readouterr().out
    assert "https://github.com/test/a: main,act-alice/feature-x" in out


def test_branches_prune_is_refused_in_remote_mode(monkeypatch, capsys):
    monkeypatch.setattr(cli_module, "_srv", lambda: _stub(code_indexed_branches=[]))
    monkeypatch.setattr(cli_module, "_is_remote", lambda: True)

    with pytest.raises(SystemExit):
        cli_module.branches(prune=True)

    assert "does not share" in capsys.readouterr().out


def test_branches_prune_is_refused_against_a_shared_store(monkeypatch, capsys):
    """Remote MCP dispatch and a remote STORE are independent: either can be
    remote without the other. A shared graph's branches belong to every user
    of it, so "this checkout has no such git branch" is not evidence the
    branch is dead.
    """
    from witan_code import repo as repo_module

    repo = "https://github.com/test/a"
    srv = _stub(
        code_indexed_branches=[
            {
                "repo": repo,
                "views": [
                    {
                        "view": "act-bob/someone-elses",
                        "branch": "someone-elses",
                        "actor": "act-bob",
                    }
                ],
            }
        ]
    )
    monkeypatch.setattr(cli_module, "_srv", lambda: srv)
    monkeypatch.setattr(cli_module, "_is_remote", lambda: False)
    monkeypatch.setattr(repo_module, "detect", lambda **_kw: repo)
    monkeypatch.setattr(repo_module, "local_branches", lambda: frozenset({"mine"}))

    def _boom(_name):
        raise AssertionError("pruned a branch on a shared graph")

    remote_client = SimpleNamespace(is_remote=True, delete_branch=_boom)
    monkeypatch.setattr(cli_module, "_branch_client", lambda _repo: remote_client)

    cli_module.branches(prune=True)

    assert "refusing to prune a shared graph" in capsys.readouterr().out


def test_rebuild_refuses_a_path_that_is_not_the_repo_root(
    tmp_path, monkeypatch, capsys
):
    """A rebuild deletes the whole store, so it must reindex the whole repo.

    `reindex src/ --rebuild` would empty the store and refill only `src/`,
    leaving the rest of the repo unindexed with nothing afterwards reporting
    it — a worse state than the unreadable store it started from.
    """
    from witan_code import repo as repo_module
    from witan_code import store as store_module

    root = tmp_path / "checkout"
    sub = root / "src"
    sub.mkdir(parents=True)
    monkeypatch.setattr(repo_module, "git_toplevel", lambda _p: root)

    def _boom(*_args, **_kwargs):
        raise AssertionError("deleted a store for a partial-path rebuild")

    monkeypatch.setattr(store_module, "discard_store", _boom)

    with pytest.raises(SystemExit):
        cli_module._rebuild_stores(sub, yes=True, slug="https://github.com/test/cg")

    out = capsys.readouterr().out
    assert "not the repo root" in out
    assert str(root) in out


def test_rebuild_refuses_a_file_path_with_real_git(tmp_path, monkeypatch, capsys):
    """`index`/`reindex` accept a FILE, and `git -C <file>` exits 128.

    Deliberately runs the real `git_toplevel` rather than stubbing it: the
    stub in the test above returns a root for anything, so it cannot see this.
    Reading "Not a directory" as "no repo" skipped the guard entirely and left
    `reindex some_file.py --rebuild` deleting the whole store and refilling it
    with one file — the exact outcome the guard exists to prevent.
    """
    import subprocess

    from witan_code import store as store_module

    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    target = root / "a.py"
    target.write_text("x = 1\n")

    def _boom(*_args, **_kwargs):
        raise AssertionError("deleted a store for a single-file rebuild")

    monkeypatch.setattr(store_module, "discard_store", _boom)

    with pytest.raises(SystemExit):
        cli_module._rebuild_stores(target, yes=True, slug="https://github.com/test/cg")

    assert "not the repo root" in capsys.readouterr().out
