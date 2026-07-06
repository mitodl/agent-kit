"""Tests for Stage 2 cross-repo symbol stitching (docs/STAGE2_STITCHING.md).

Unit tests exercise witan_code.stitch.resolve() directly with hand-built
RepoSymbol-shaped dicts (no omnigraph/tree-sitter needed); integration tests
verify the full path through indexing, the bridge store, the MCP tools, and
the CLI.
"""

from witan_code.stitch import PreciseEdge, resolve

from .conftest import requires_stack


def _row(
    repo,
    role,
    symbol,
    scheme,
    descriptor,
    key_norm,
    *,
    version=".",
    kind="env_var",
):
    return {
        "repo": repo,
        "role": role,
        "symbol": symbol,
        "scheme": scheme,
        "descriptor": descriptor,
        "key_norm": key_norm,
        "version": version,
        "kind": kind,
    }


# ── Core join: env/svc (exact descriptor) ─────────────────────────


def test_env_var_resolves_to_single_precise_edge():
    ext = _row("repo-a", "external", "env:.:.:.:FOO", "env", "FOO", "FOO")
    exp = _row(
        "repo-b", "exported", "env:.:b:main:FOO", "env", "FOO", "FOO", version="main"
    )
    edges, unresolved = resolve([ext, exp])
    assert unresolved == []
    assert len(edges) == 1
    e = edges[0]
    assert isinstance(e, PreciseEdge)
    assert (e.consumer_repo, e.provider_repo) == ("repo-a", "repo-b")
    assert e.match_count == 1
    assert e.preferred
    assert not e.ambiguous_version


def test_no_match_goes_to_unresolved():
    ext = _row("repo-a", "external", "env:.:.:.:FOO", "env", "FOO", "FOO")
    edges, unresolved = resolve([ext])
    assert edges == []
    assert unresolved == [ext]


def test_self_repo_export_does_not_join():
    """A repo that both consumes and provides the same key must not link to itself."""
    ext = _row("repo-a", "external", "env:.:.:.:FOO", "env", "FOO", "FOO")
    exp = _row(
        "repo-a", "exported", "env:.:a:main:FOO", "env", "FOO", "FOO", version="main"
    )
    edges, unresolved = resolve([ext, exp])
    assert edges == []
    assert unresolved == [ext]


# ── http: key_norm join + method compatibility ────────────────────


def test_http_wildcard_consumer_matches_any_method():
    ext = _row(
        "repo-a",
        "external",
        "http:.:.:.:* /api/v0/x/{}",
        "http",
        "* /api/v0/x/{}",
        "/api/v0/x/{}",
    )
    exp = _row(
        "repo-b",
        "exported",
        "http:pypi:b:main:GET /api/v0/x/{}",
        "http",
        "GET /api/v0/x/{}",
        "/api/v0/x/{}",
        version="main",
    )
    edges, unresolved = resolve([ext, exp])
    assert unresolved == []
    assert len(edges) == 1


def test_http_mismatched_method_does_not_join():
    ext = _row(
        "repo-a",
        "external",
        "http:.:.:.:POST /api/v0/x/{}",
        "http",
        "POST /api/v0/x/{}",
        "/api/v0/x/{}",
    )
    exp = _row(
        "repo-b",
        "exported",
        "http:pypi:b:main:GET /api/v0/x/{}",
        "http",
        "GET /api/v0/x/{}",
        "/api/v0/x/{}",
        version="main",
    )
    edges, unresolved = resolve([ext, exp])
    assert edges == []
    assert unresolved == [ext]


def test_http_matching_method_joins():
    ext = _row(
        "repo-a",
        "external",
        "http:.:.:.:GET /api/v0/x/{}",
        "http",
        "GET /api/v0/x/{}",
        "/api/v0/x/{}",
    )
    exp = _row(
        "repo-b",
        "exported",
        "http:pypi:b:main:GET /api/v0/x/{}",
        "http",
        "GET /api/v0/x/{}",
        "/api/v0/x/{}",
        version="main",
    )
    edges, unresolved = resolve([ext, exp])
    assert unresolved == []
    assert len(edges) == 1


# ── pkg: key_norm join (descriptor is always ".") ─────────────────


def test_package_joins_via_key_norm():
    ext = _row(
        "repo-a",
        "external",
        "pkg:npm:@mitodl/foo:.:.",
        "pkg",
        ".",
        "@mitodl/foo",
        kind="package",
    )
    exp = _row(
        "repo-b",
        "exported",
        "pkg:npm:@mitodl/foo:main:.",
        "pkg",
        ".",
        "@mitodl/foo",
        version="main",
        kind="package",
    )
    edges, unresolved = resolve([ext, exp])
    assert unresolved == []
    assert len(edges) == 1


def test_different_packages_share_descriptor_but_do_not_join():
    """Both pkg symbols have descriptor "." — key_norm must disambiguate them."""
    ext = _row(
        "repo-a",
        "external",
        "pkg:npm:@mitodl/foo:.:.",
        "pkg",
        ".",
        "@mitodl/foo",
        kind="package",
    )
    exp = _row(
        "repo-b",
        "exported",
        "pkg:npm:@mitodl/bar:main:.",
        "pkg",
        ".",
        "@mitodl/bar",
        version="main",
        kind="package",
    )
    edges, unresolved = resolve([ext, exp])
    assert edges == []
    assert unresolved == [ext]


# ── Multi-match + version disambiguation (SYMBOL_FORMAT.md decision 1) ──


def test_multiple_providers_all_returned_as_edges():
    ext = _row("repo-a", "external", "env:.:.:.:FOO", "env", "FOO", "FOO")
    exp_b = _row(
        "repo-b", "exported", "env:.:b:main:FOO", "env", "FOO", "FOO", version="main"
    )
    exp_c = _row(
        "repo-c", "exported", "env:.:c:main:FOO", "env", "FOO", "FOO", version="main"
    )
    edges, unresolved = resolve([ext, exp_b, exp_c])
    assert unresolved == []
    assert len(edges) == 2
    assert {e.provider_repo for e in edges} == {"repo-b", "repo-c"}
    for e in edges:
        assert e.match_count == 2
        # No consumer-specified version and two "main" candidates: ambiguous,
        # but both still preferred (no guessing a winner).
        assert e.ambiguous_version
        assert e.preferred


def test_exact_consumer_version_prefers_matching_provider():
    ext = _row(
        "repo-a", "external", "env:.:.:v2:FOO", "env", "FOO", "FOO", version="v2"
    )
    exp_v1 = _row(
        "repo-b", "exported", "env:.:b:v1:FOO", "env", "FOO", "FOO", version="v1"
    )
    exp_v2 = _row(
        "repo-c", "exported", "env:.:c:v2:FOO", "env", "FOO", "FOO", version="v2"
    )
    edges, unresolved = resolve([ext, exp_v1, exp_v2])
    assert unresolved == []
    assert len(edges) == 2
    by_repo = {e.provider_repo: e for e in edges}
    assert by_repo["repo-c"].preferred and not by_repo["repo-c"].ambiguous_version
    assert not by_repo["repo-b"].preferred and not by_repo["repo-b"].ambiguous_version


def test_no_exact_version_falls_back_to_main():
    ext = _row(
        "repo-a", "external", "env:.:.:v3:FOO", "env", "FOO", "FOO", version="v3"
    )
    exp_v1 = _row(
        "repo-b", "exported", "env:.:b:v1:FOO", "env", "FOO", "FOO", version="v1"
    )
    exp_main = _row(
        "repo-c", "exported", "env:.:c:main:FOO", "env", "FOO", "FOO", version="main"
    )
    edges, unresolved = resolve([ext, exp_v1, exp_main])
    assert unresolved == []
    by_repo = {e.provider_repo: e for e in edges}
    assert by_repo["repo-c"].preferred and not by_repo["repo-c"].ambiguous_version
    assert not by_repo["repo-b"].preferred


def test_none_descriptor_and_version_handled_gracefully():
    """Legacy/malformed store rows may carry None instead of "." — must not crash."""
    ext = _row(
        "repo-a",
        "external",
        "http:.:.:.:GET /api/v0/x/{}",
        "http",
        None,
        "/api/v0/x/{}",
        version=None,
    )
    exp = _row(
        "repo-b",
        "exported",
        "http:pypi:b:main:GET /api/v0/x/{}",
        "http",
        "GET /api/v0/x/{}",
        "/api/v0/x/{}",
        version="main",
    )
    edges, unresolved = resolve([ext, exp])
    assert unresolved == []
    assert len(edges) == 1
    assert edges[0].provider_repo == "repo-b"
    assert edges[0].preferred


# ── Integration: full store → MCP tools → CLI ─────────────────────


A_SETTINGS = """\
from main.envs import get_string

APP_BASE_URL = get_string("MITOL_APP_BASE_URL", None)
"""

A_CLIENT_TS = """\
export async function listCourses(id) {
  const path = `/api/v1/courses/${id}/`
  return fetch(path)
}
"""

B_PULUMI = """\
config:
  learn_ai:env_vars:
    MITOL_APP_BASE_URL: "https://learn-ai-qa.ol.mit.edu"
"""

B_OPENAPI = (
    '{"openapi": "3.0.0", "paths": '
    '{"/api/v1/courses/{id}/": {"get": {"operationId": "courses_retrieve"}}}}'
)


def _fn(tool):
    return getattr(tool, "fn", tool)


@requires_stack
def test_code_precise_edges_and_unresolved_symbols(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))

    a = tmp_path / "repo_a"
    (a / "main").mkdir(parents=True)
    (a / "main" / "settings.py").write_text(A_SETTINGS)
    (a / "client.ts").write_text(A_CLIENT_TS)
    b = tmp_path / "repo_b"
    b.mkdir()
    (b / "Pulumi.QA.yaml").write_text(B_PULUMI)
    (b / "openapi.json").write_text(B_OPENAPI)
    # repo-c consumes an endpoint no one provides — an indexing-coverage gap.
    c = tmp_path / "repo_c"
    c.mkdir()
    (c / "client.ts").write_text(
        "export function f() { return fetch('/api/v0/nope/'); }\n"
    )

    for repo, path in (
        ("https://github.com/test/repo-a", a),
        ("https://github.com/test/repo-b", b),
        ("https://github.com/test/repo-c", c),
    ):
        monkeypatch.setenv("WITAN_REPO", repo)
        monkeypatch.setattr(srv, "cfg", cfg_mod.load())
        srv._clients.clear()
        indexer.index_path(path, config=srv.cfg)

    edges = _fn(srv.code_precise_edges)()
    assert any(
        e["consumer_repo"] == "https://github.com/test/repo-a"
        and e["provider_repo"] == "https://github.com/test/repo-b"
        and e["kind"] == "env_var"
        for e in edges
    )
    assert any(
        e["consumer_repo"] == "https://github.com/test/repo-a"
        and e["provider_repo"] == "https://github.com/test/repo-b"
        and e["kind"] == "endpoint"
        for e in edges
    )

    scoped = _fn(srv.code_precise_edges)(repo="https://github.com/test/repo-a")
    assert scoped and all(
        "https://github.com/test/repo-a" in (e["consumer_repo"], e["provider_repo"])
        for e in scoped
    )

    unresolved = _fn(srv.code_unresolved_symbols)()
    assert any(
        r["repo"] == "https://github.com/test/repo-c" and "nope" in r["symbol"]
        for r in unresolved
    )
    scoped_unresolved = _fn(srv.code_unresolved_symbols)(
        repo="https://github.com/test/repo-c"
    )
    assert scoped_unresolved and all(
        r["repo"] == "https://github.com/test/repo-c" for r in scoped_unresolved
    )


@requires_stack
def test_tools_empty_without_bridge_store(tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/none")
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    assert _fn(srv.code_precise_edges)() == []
    assert _fn(srv.code_unresolved_symbols)() == []
