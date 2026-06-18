"""End-to-end tests for the cross-repo context bridge.

Index two throwaway repos into ONE shared bridge store and assert that
provider/consumer linkages join across repos by contract key.
"""

from .conftest import requires_stack

# Repo A (mit-learn-like): a Django settings consumer + a NextJS endpoint/env
# consumer.
A_SETTINGS = """\
from main.envs import get_string

APP_BASE_URL = get_string("MITOL_APP_BASE_URL", None)
DEBUG = get_string("DEBUG", "false")
"""

A_CLIENT_TS = """\
export async function listCourses(id) {
  const path = `/api/v1/courses/${id}/`
  const base = process.env.NEXT_PUBLIC_MITX_ONLINE_BASE_URL
  return fetch(base + path)
}
"""

# Repo B (ol-infrastructure-like): a Pulumi stack config that sets the env var,
# plus an OpenAPI spec that serves the endpoint.
B_PULUMI = """\
config:
  learn_ai:env_vars:
    MITOL_APP_BASE_URL: "https://learn-ai-qa.ol.mit.edu"
    AI_DEFAULT_TUTOR_MODEL: "openai/gpt-4o"
"""

B_OPENAPI = (
    '{"openapi": "3.0.0", "paths": '
    '{"/api/v1/courses/{id}/": {"get": {"operationId": "courses_retrieve"}}}}'
)


def _fn(tool):
    return getattr(tool, "fn", tool)


def _index(srv, indexer, path):
    return _fn(srv.code_reindex)(path=str(path))


def _make_repos(tmp_path):
    a = tmp_path / "repo_a"
    (a / "main").mkdir(parents=True)
    (a / "main" / "settings.py").write_text(A_SETTINGS)
    (a / "client.ts").write_text(A_CLIENT_TS)

    b = tmp_path / "repo_b"
    b.mkdir()
    (b / "Pulumi.QA.yaml").write_text(B_PULUMI)
    (b / "openapi.json").write_text(B_OPENAPI)
    return a, b


@requires_stack
def test_cross_repo_env_and_endpoint_linkage(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    a, b = _make_repos(tmp_path)

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/repo-a")
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    stats_a = _index(srv, indexer, a)
    assert stats_a["bindings"] > 0

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/repo-b")
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    stats_b = _index(srv, indexer, b)
    assert stats_b["bindings"] > 0

    # env_var: repo-b (Pulumi) provides MITOL_APP_BASE_URL; repo-a (Django) consumes it.
    providers = _fn(srv.code_interface_providers)("env_var", "MITOL_APP_BASE_URL")
    assert {p["repo"] for p in providers} == {"https://github.com/test/repo-b"}

    consumers = _fn(srv.code_interface_consumers)("env_var", "MITOL_APP_BASE_URL")
    assert {c["repo"] for c in consumers} == {"https://github.com/test/repo-a"}

    # endpoint: openapi provider path joins the templated TS consumer path.
    ep_providers = _fn(srv.code_interface_providers)(
        "endpoint", "/api/v1/courses/{id}/"
    )
    assert any(p["repo"] == "https://github.com/test/repo-b" for p in ep_providers)
    ep_consumers = _fn(srv.code_interface_consumers)(
        "endpoint", "/api/v1/courses/{id}/"
    )
    assert any(c["repo"] == "https://github.com/test/repo-a" for c in ep_consumers)


@requires_stack
def test_cross_repo_impact_and_generic_flag(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    a, b = _make_repos(tmp_path)

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/repo-a")
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    _index(srv, indexer, a)

    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/repo-b")
    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    _index(srv, indexer, b)

    # Use the repo-a consumer binding's symbol_id to compute cross-repo impact.
    consumers = _fn(srv.code_interface_consumers)("env_var", "MITOL_APP_BASE_URL")
    symbol_id = consumers[0]["symbol_id"]
    assert symbol_id

    impact = _fn(srv.code_cross_repo_impact)(symbol_id)
    assert impact["symbol_id"] == symbol_id
    assert impact["bindings"], "the symbol's own bindings"
    # repo-b's provider of MITOL_APP_BASE_URL shows up cross-repo.
    assert any(
        b["repo"] == "https://github.com/test/repo-b" for b in impact["cross_repo"]
    )

    # DEBUG is generic → flagged and excluded from cross-repo fan-out.
    debug = _fn(srv.code_interface_consumers)("env_var", "DEBUG")
    assert all(d.get("generic") == "1" for d in debug)


@requires_stack
def test_incremental_reindex_keeps_sibling_bindings(tmp_path, monkeypatch):
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/repo-a")
    a, _ = _make_repos(tmp_path)

    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    _index(srv, indexer, a)  # full-repo index

    # Re-index ONLY the TS file (narrow target → per-file bridge purge).
    _index(srv, indexer, a / "client.ts")

    # The Python settings env-var binding (a sibling file) must survive.
    consumers = _fn(srv.code_interface_consumers)("env_var", "MITOL_APP_BASE_URL")
    assert {c["repo"] for c in consumers} == {"https://github.com/test/repo-a"}


@requires_stack
def test_bridge_tools_empty_without_store(tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/none")
    from witan_code import config as cfg_mod
    from witan_code import server as srv

    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()
    assert _fn(srv.code_interface_providers)("env_var", "X") == []
    impact = _fn(srv.code_cross_repo_impact)("https://github.com/test/none#a.py::f")
    assert impact == {
        "symbol_id": "https://github.com/test/none#a.py::f",
        "bindings": [],
        "cross_repo": [],
    }
