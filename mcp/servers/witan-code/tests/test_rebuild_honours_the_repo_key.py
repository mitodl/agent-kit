"""`reindex --rebuild` resolves its repo key the same way the index does.

`--rebuild` DELETES stores before indexing, so which key it resolves is not a
display detail. Two ways that went wrong when it resolved its own:

1. It used the directory-name fallback, so `--rebuild --repo <uri>` on a
   remoteless checkout checked and deleted the legacy bare-name graph instead of
   the one it was told to use.
2. The refusal for an unkeyable target lived only inside `index_path`, which
   runs *after* the rebuild — so `--rebuild --yes` there could drop the shared
   bridge graph and only then decline to index. A run that is going to be
   refused must delete nothing.
"""

import subprocess
from pathlib import Path

import pytest

from witan_code import cli as cli_module


@pytest.fixture
def remoteless_checkout(tmp_path, monkeypatch) -> Path:
    """A real git checkout whose `origin` was never added."""
    monkeypatch.delenv("WITAN_REPO", raising=False)
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    root = tmp_path / "scratchproj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.fixture
def never_deletes(monkeypatch):
    """Fail loudly if any store is discarded."""
    from witan_code import store as store_module

    def _boom(*_args, **_kwargs):
        raise AssertionError("deleted a store on a run that must delete nothing")

    monkeypatch.setattr(store_module, "discard_store", _boom)


def test_rebuild_uses_the_explicit_repo_not_the_directory_name(
    remoteless_checkout, monkeypatch
):
    from witan_code import store as store_module

    asked: list[str] = []
    monkeypatch.setattr(
        store_module,
        "store_for_repo",
        lambda slug, _cfg: asked.append(slug) or _Missing(),
    )
    monkeypatch.setattr(store_module, "bridge_store", lambda _cfg: _Missing())

    cli_module._rebuild_stores(
        remoteless_checkout, yes=True, slug="https://github.com/test/cg"
    )

    # Not "scratchproj" — the directory name the old fallback would have used.
    assert asked == ["https://github.com/test/cg"]


def test_an_unkeyable_target_is_refused_before_anything_is_deleted(
    remoteless_checkout, never_deletes, capsys
):
    """The ordering half. `--yes` skips the confirmation prompt, so nothing else
    stands between the old code and a deleted bridge graph."""
    with pytest.raises(SystemExit):
        cli_module.reindex(remoteless_checkout, rebuild=True, yes=True)

    assert "no git remote" in capsys.readouterr().err


def test_the_refusal_names_both_opt_ins(remoteless_checkout, never_deletes, capsys):
    with pytest.raises(SystemExit):
        cli_module.reindex(remoteless_checkout, rebuild=True, yes=True)

    err = capsys.readouterr().err
    assert "--repo" in err
    assert "WITAN_REPO" in err


class _Missing:
    """A store ref that does not exist, so nothing is a rebuild candidate."""

    def exists(self, _cfg=None):
        return False
