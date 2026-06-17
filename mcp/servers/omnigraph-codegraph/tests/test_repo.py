"""Unit tests for codegraph repo detection (no binary required)."""

import pytest

from omnigraph_codegraph import repo


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:mitodl/ol-django.git", "https://github.com/mitodl/ol-django"),
        ("https://github.com/mitodl/ol-django", "https://github.com/mitodl/ol-django"),
        ("ssh://git@github.com/mitodl/repo.git", "https://github.com/mitodl/repo"),
    ],
)
def test_normalise_matches_memory_layer(url, expected):
    # Must stay identical to omnigraph_memory.repo._normalise so symbol ids and
    # the Layer-1 symbol_refs that point at them share one repo key.
    assert repo._normalise(url) == expected


def test_detect_env_override(monkeypatch):
    monkeypatch.setenv("OMNIGRAPH_CODEGRAPH_REPO", "https://github.com/test/cg")
    assert repo.detect() == "https://github.com/test/cg"
