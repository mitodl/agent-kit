"""Golden contract test for the cross-layer repo-key canonicalizer.

This is THE regression guard that makes a single source of truth safe: the
memory/workflow/task layer and the code-graph layer both derive the repo key
from ``normalise``; if its output ever changes, symbol ids (``repo#path::Name``)
and the ``symbol_refs`` that point at them stop joining. Lock the canonical form
here.
"""

import pytest

from witan_core import repo_key


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # SSH scp-style
        ("git@github.com:mitodl/ol-django.git", "https://github.com/mitodl/ol-django"),
        # already canonical
        ("https://github.com/mitodl/ol-django", "https://github.com/mitodl/ol-django"),
        # trailing .git stripped
        (
            "https://github.com/mitodl/ol-django.git",
            "https://github.com/mitodl/ol-django",
        ),
        # gitlab subgroups (multi-segment path preserved)
        ("git@gitlab.com:grp/sub/repo.git", "https://gitlab.com/grp/sub/repo"),
        # https userinfo (token) dropped
        (
            "https://x-token@github.com/mitodl/repo.git",
            "https://github.com/mitodl/repo",
        ),
        # ssh:// scheme prefix
        ("ssh://git@github.com/mitodl/repo.git", "https://github.com/mitodl/repo"),
        # trailing slash stripped
        ("https://github.com/mitodl/repo/", "https://github.com/mitodl/repo"),
        # trailing slash *after* .git — .git must still be stripped
        ("https://github.com/mitodl/repo.git/", "https://github.com/mitodl/repo"),
        ("git@github.com:mitodl/repo.git/", "https://github.com/mitodl/repo"),
        # unknown format returned as-is
        ("some-bare-string", "some-bare-string"),
    ],
)
def test_normalise_to_canonical_https(url, expected):
    assert repo_key.normalise(url) == expected


def test_find_git_config_walks_up(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    assert repo_key.find_git_config(nested) == tmp_path / ".git" / "config"


def test_find_git_config_returns_none_when_absent(tmp_path):
    assert repo_key.find_git_config(tmp_path) is None
