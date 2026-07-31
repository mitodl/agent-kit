"""witan-code-subclass-specific OmnigraphClient tests.

The generic base machinery (_find_binary lookup order, OCC conflict surfacing,
admission-cap backoff) is covered in packages/witan-core/tests/test_omnigraph.py.
Here we only assert witan-code's own subclass tail: the setup-hint in the
binary-not-found message. (branch ops + bulk load are exercised against a real
store in test_branches.py / test_indexer.py.)
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

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


def _check(*, remote: bool, branch: str | None, role: str) -> None:
    check_writable(
        client=SimpleNamespace(is_remote=remote),
        branch=branch,
        cfg=_cfg(role),
        slug="https://github.com/test/a",
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


def test_shared_branch_view_is_not_the_default_view():
    """A branch-scoped write is isolated from the view everyone reads, so it
    is not what this gate is protecting."""
    _check(remote=True, branch="feature-x", role=cfg_module.INDEX_ROLE_CLIENT)


def test_refusal_names_the_way_out():
    with pytest.raises(SharedGraphWriteRefused) as excinfo:
        _check(remote=True, branch=None, role=cfg_module.INDEX_ROLE_CLIENT)
    assert "WITAN_CODE_INDEX_ROLE=ci" in str(excinfo.value)
