"""The server half of writing a code graph through the MCP tier (ADR-0005 c).

The tier's job is to be the *authority* on two things the client only advises
on: who the caller is, and whether they own the view they are writing. These
pin both, plus the surface's refusals — an unprovisioned actor, a query file
that is not one of ours, a graph the deployment does not serve.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from witan_code import config as cfg_module
from witan_code import ingest

from .conftest import requires_stack

REPO = "https://github.com/test/cg"


@pytest.fixture(autouse=True)
def _not_a_deployment(monkeypatch):
    """Default every test to local-stdio identity; deployment tests opt in."""
    monkeypatch.delenv("WITAN_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("WITAN_ACTOR_TOKENS_FILE", raising=False)
    monkeypatch.delenv(ingest.STORE_TOOLS_ENV_VAR, raising=False)


def _token(sub: str):
    return SimpleNamespace(claims={"sub": sub})


# ── Registration ──────────────────────────────────────────────────────────────


def test_store_tools_are_off_for_a_local_server(monkeypatch):
    """A local stdio server serves no remote indexer, so it offers no store
    tools — one of which runs named mutations."""
    assert ingest.store_tools_enabled() is False


def test_store_tools_are_on_for_a_deployment(monkeypatch):
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/ol")
    assert ingest.store_tools_enabled() is True


def test_the_override_wins_both_ways(monkeypatch):
    monkeypatch.setenv(ingest.STORE_TOOLS_ENV_VAR, "1")
    assert ingest.store_tools_enabled() is True
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/ol")
    monkeypatch.setenv(ingest.STORE_TOOLS_ENV_VAR, "0")
    assert ingest.store_tools_enabled() is False


# ── Identity ──────────────────────────────────────────────────────────────────


def test_a_deployment_reads_the_actor_off_the_validated_jwt(monkeypatch):
    """The whole point of routing writes here: the actor is the token's, not
    whatever the client resolved for itself."""
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/ol")
    monkeypatch.setenv("WITAN_ACTOR", "act-someone-else")
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token", lambda: _token("Alice-42")
    )
    assert ingest.request_actor() == "act-alice-42"


def test_a_deployment_without_a_request_falls_back_to_the_process(monkeypatch):
    """An admin/migration command inside the container has no caller to be."""
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/ol")
    monkeypatch.setenv("WITAN_ACTOR", "act-admin")
    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", lambda: None)
    assert ingest.request_actor() == "act-admin"


def test_a_local_server_writes_as_itself(monkeypatch):
    monkeypatch.setenv("WITAN_ACTOR", "act-local")
    assert ingest.request_actor() == "act-local"


@requires_stack
def test_an_unprovisioned_actor_is_refused_rather_than_served_as_the_service(
    tmp_path, monkeypatch
):
    """Falling back to the server's own omnigraph token would attribute the
    caller's records to the service account — the attribution this exists to
    prevent."""
    tokens = tmp_path / "actor-tokens.json"
    tokens.write_text(json.dumps({"act-provisioned": "tok-1"}))
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/ol")
    monkeypatch.setenv("WITAN_ACTOR_TOKENS_FILE", str(tokens))
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token", lambda: _token("stranger")
    )
    with pytest.raises(ingest.IngestRefused, match="No omnigraph bearer token"):
        ingest.views(REPO)


@requires_stack
def test_the_callers_own_token_addresses_the_data_tier(tmp_path, monkeypatch):
    tokens = tmp_path / "actor-tokens.json"
    tokens.write_text(json.dumps({"act-alice": "tok-alice"}))
    monkeypatch.setenv("WITAN_OIDC_ISSUER", "https://sso.example.org/realms/ol")
    monkeypatch.setenv("WITAN_ACTOR_TOKENS_FILE", str(tokens))
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token", lambda: _token("alice")
    )
    seen: dict[str, str | None] = {}
    monkeypatch.setattr(
        ingest.store_module.StoreRef,
        "client",
        lambda self, *a, **kw: seen.setdefault("token", self.token) and None,
    )
    ingest._client(REPO, None, cfg_module.load(), "act-alice")
    assert seen["token"] == "tok-alice"


# ── Authorization ─────────────────────────────────────────────────────────────


def test_a_view_another_actor_owns_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    with pytest.raises(ingest.IngestRefused, match="owned by act-bob"):
        ingest._authorize(REPO, "act-bob/feature_x", cfg_module.load(), "act-alice")


def test_your_own_view_is_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    ingest._authorize(REPO, "act-alice/feature_x", cfg_module.load(), "act-alice")


def test_the_shared_default_view_belongs_to_ci(monkeypatch, tmp_path):
    """`branch=None` is the view everyone reads. Only the declared CI indexer
    writes it, whoever is asking."""
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    with pytest.raises(ingest.IngestRefused, match="owned by CI"):
        ingest._authorize(REPO, None, cfg_module.load(), "act-alice")

    monkeypatch.setenv("WITAN_CODE_INDEX_ROLE", cfg_module.INDEX_ROLE_CI)
    ingest._authorize(REPO, None, cfg_module.load(), "act-ci")


# ── Surface ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query", ["../../../secrets.gq", "delete.gq/x", "code_read", "nope.gq"]
)
def test_only_bundled_query_files_can_be_named(query, tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    with pytest.raises(ingest.IngestRefused):
        ingest._query_path(cfg_module.load(), query)


@requires_stack
def test_a_write_and_a_read_round_trip_through_the_surface(sample_repo, monkeypatch):
    """Records loaded through the tier are readable back through it."""
    from witan_code import indexer

    monkeypatch.setenv("WITAN_ACTOR", "act-alice")
    indexer.index_path(sample_repo, config=cfg_module.load())

    rows = ingest.read(REPO, None, "code_read.gq", "all_file_hashes", {})
    assert {row["slug"] for row in rows}

    view = ingest.open_view(REPO, "act-alice/feature_x")
    written = ingest.load_records(
        REPO,
        view,
        [
            {
                "type": "CodeFile",
                "data": {
                    "slug": f"{REPO}#src/added.py",
                    "repo": REPO,
                    "path": "src/added.py",
                    "language": "python",
                    "content_hash": "deadbeef",
                    "indexed_at": "2026-08-01T00:00:00Z",
                },
            }
        ],
    )
    assert written == 1
    after = ingest.read(REPO, view, "code_read.gq", "all_file_hashes", {})
    assert f"{REPO}#src/added.py" in {row["slug"] for row in after}
    # The view is a view: the shared one it forked from never saw the write.
    on_main = ingest.read(REPO, None, "code_read.gq", "all_file_hashes", {})
    assert f"{REPO}#src/added.py" not in {row["slug"] for row in on_main}


# ── Batched mutation ──────────────────────────────────────────────────────────


@requires_stack
def test_a_batch_of_deletes_applies_every_step(sample_repo, monkeypatch):
    """The server half of the batch: the client sends params, the splice and
    the queries_dir lookup stay here, and all of it lands."""
    from witan_code import indexer

    monkeypatch.setenv("WITAN_ACTOR", "act-alice")
    indexer.index_path(sample_repo, config=cfg_module.load())

    view = ingest.open_view(REPO, "act-alice/feature_x")
    before = {
        row["slug"]
        for row in ingest.read(REPO, view, "code_read.gq", "all_file_hashes", {})
    }
    assert before, "nothing indexed, so the delete would prove nothing"

    applied = ingest.mutate_many(
        REPO,
        view,
        [
            {"query": "delete.gq", "name": "delete_file", "params": {"id": slug}}
            for slug in sorted(before)
        ],
    )
    assert applied == len(before)
    after = {
        row["slug"]
        for row in ingest.read(REPO, view, "code_read.gq", "all_file_hashes", {})
    }
    assert after == set()


@requires_stack
def test_a_bad_step_refuses_the_whole_batch_and_commits_no_prefix(
    sample_repo, monkeypatch
):
    """Validation runs over every step before any of them does. Otherwise a
    typo in step 300 leaves 299 deletes applied and the caller told it failed."""
    from witan_code import indexer

    monkeypatch.setenv("WITAN_ACTOR", "act-alice")
    indexer.index_path(sample_repo, config=cfg_module.load())

    view = ingest.open_view(REPO, "act-alice/feature_x")
    before = {
        row["slug"]
        for row in ingest.read(REPO, view, "code_read.gq", "all_file_hashes", {})
    }
    good = [
        {"query": "delete.gq", "name": "delete_file", "params": {"id": slug}}
        for slug in sorted(before)
    ]
    with pytest.raises(ingest.IngestRefused):
        ingest.mutate_many(
            REPO, view, [*good, {"query": "../etc/passwd.gq", "name": "x"}]
        )

    after = {
        row["slug"]
        for row in ingest.read(REPO, view, "code_read.gq", "all_file_hashes", {})
    }
    assert after == before


@pytest.mark.parametrize(
    "step",
    [
        {"name": "delete_file", "params": {}},
        {"query": "delete.gq", "params": {}},
        {"query": "", "name": "delete_file"},
        {"query": "delete.gq", "name": ""},
        {"query": "delete.gq", "name": 7},
    ],
)
def test_a_malformed_step_is_refused_by_name(step, tmp_path, monkeypatch):
    """The steps arrive as free-form dicts off the wire, so say which field is
    wrong rather than raising a KeyError from inside the splice.

    The actor owns the view here deliberately: authorization runs BEFORE step
    validation, so without it every case would refuse for the wrong reason.
    """
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    monkeypatch.setenv("WITAN_ACTOR", "act-alice")
    with pytest.raises(ingest.IngestRefused, match="non-empty"):
        ingest.mutate_many(REPO, "act-alice/x", [step])


@requires_stack
def test_a_batch_into_someone_elses_view_is_refused(sample_repo, monkeypatch):
    """`mutate_many` is a write, so it goes through the same ownership gate
    every other write does — being a batch does not route around it."""
    from witan_code import indexer

    monkeypatch.setenv("WITAN_ACTOR", "act-alice")
    indexer.index_path(sample_repo, config=cfg_module.load())
    with pytest.raises(ingest.IngestRefused, match="owned by act-bob"):
        ingest.mutate_many(
            REPO,
            "act-bob/feature_x",
            [{"query": "delete.gq", "name": "delete_file", "params": {"id": "x"}}],
        )


@requires_stack
def test_the_graph_listing_names_repos_not_ids(sample_repo):
    """A client cannot invert `graph_id`, so the server answers in repo URIs."""
    from witan_code import indexer

    indexer.index_path(sample_repo, config=cfg_module.load())
    assert ingest.graphs() == [REPO]


def test_a_graph_the_cluster_does_not_declare_fails_on_the_first_call(monkeypatch):
    """Not on the thousandth record. A client cannot create a cluster graph —
    provisioning declares them — so a run that continues writes nowhere."""
    from witan_code import store as store_module

    monkeypatch.setenv("WITAN_CODE_SERVER", "https://omnigraph.example.org")

    def _not_served(self):
        raise RuntimeError(f"graph '{self.graph_id}' not found")

    monkeypatch.setattr(store_module.OmnigraphClient, "list_branches", _not_served)
    with pytest.raises(store_module.ClusterGraphMissing, match="not served"):
        ingest.read(
            "https://github.com/test/never-provisioned",
            None,
            "code_read.gq",
            "all_file_hashes",
            {},
        )
