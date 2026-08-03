"""witan-code-subclass-specific OmnigraphClient tests.

The generic base machinery (_find_binary lookup order, OCC conflict surfacing,
admission-cap backoff) is covered in packages/witan-core/tests/test_omnigraph.py.
Here we only assert witan-code's own subclass tail: the setup-hint in the
binary-not-found message. (branch ops + bulk load are exercised against a real
store in test_branches.py / test_indexer.py.)
"""

import shutil
from pathlib import Path

import pytest

from witan_code import config as cfg_module
from witan_code.graph import (
    OmnigraphClient,
    SharedGraphWriteRefused,
    check_writable,
)


def test_find_binary_message_names_witan_code_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="witan-code setup"):
        OmnigraphClient._find_binary()


# ── check_writable: who may write a shared graph's default-branch view ───────
#
# On the cluster, a per-repo code graph is one graph for the whole team and its
# default-branch view is indexed by CI. `branch is None` is what makes a write
# target that view.


def _cfg(role: str = cfg_module.INDEX_ROLE_CLIENT) -> cfg_module.Config:
    return cfg_module.Config(
        code_dir=Path("/code"),
        author="test",
        queries_dir=Path("/queries"),
        schema_file=Path("/schema.pg"),
        bridge_schema_file=Path("/bridge.pg"),
        index_role=role,
    )


def _check(
    *,
    remote: bool,
    branch: str | None,
    role: str = cfg_module.INDEX_ROLE_CLIENT,
    actor: str | None = "act-alice",
) -> None:
    check_writable(
        is_remote=remote,
        branch=branch,
        cfg=_cfg(role),
        slug="https://github.com/test/a",
        actor=actor,
    )


def test_local_store_writes_its_main_view_whatever_the_role():
    """A local store has one user, who is its writer. The role is about the
    shared graph and must not gate the single-machine case."""
    _check(remote=False, branch=None, role=cfg_module.INDEX_ROLE_CLIENT)


def test_shared_default_branch_view_refused_without_the_writer_role():
    with pytest.raises(SharedGraphWriteRefused, match="owned by CI"):
        _check(remote=True, branch=None, role=cfg_module.INDEX_ROLE_CLIENT)


def test_designated_writer_may_write_the_shared_default_branch_view():
    """The CI indexer is remote too — authority comes from the declared role,
    so a blanket "refuse when remote" would block the one writer there is."""
    _check(remote=True, branch=None, role=cfg_module.INDEX_ROLE_CI)


def test_refusal_names_the_way_out():
    with pytest.raises(SharedGraphWriteRefused) as excinfo:
        _check(remote=True, branch=None, role=cfg_module.INDEX_ROLE_CLIENT)
    assert "WITAN_CODE_INDEX_ROLE=ci" in str(excinfo.value)


# ── Branch views: readable by everyone, writable only by their owner ─────────
#
# Branch views live ON the shared graph, so isolation cannot come from where
# they live. It comes from the name — `<actor>/<branch>` — and this gate.


def test_an_actor_may_write_its_own_branch_view():
    """No role needed: a view prefixed with your own actor cannot reach the
    default view everyone falls back to."""
    _check(remote=True, branch="act-alice/feature-x", actor="act-alice")


def test_another_actors_branch_view_is_refused():
    """The collision this whole scheme exists to prevent, in its explicit
    form: writing where the name says someone else writes."""
    with pytest.raises(SharedGraphWriteRefused, match="owned by act-bob"):
        _check(remote=True, branch="act-bob/feature-x", actor="act-alice")


def test_an_unnamespaced_branch_view_is_refused_on_a_shared_graph():
    """`feature-x` with no owner is what two checkouts on the same git branch
    used to share — and overwrite."""
    with pytest.raises(SharedGraphWriteRefused, match="owned by nobody"):
        _check(remote=True, branch="feature-x", actor="act-alice")


def test_a_shared_branch_view_needs_an_identity_to_own_it():
    """No login, no ownership. The message has to say so — the alternative is
    an un-owned view landing on the shared graph."""
    with pytest.raises(SharedGraphWriteRefused, match="witan login"):
        _check(remote=True, branch="feature-x", actor=None)


def test_local_branch_views_need_no_actor():
    """A local store has one user, who owns every view in it — unchanged
    names, no migration, no login required to index offline."""
    _check(remote=False, branch="feature-x", actor=None)
