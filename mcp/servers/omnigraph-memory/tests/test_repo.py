"""Unit tests for repo-slug normalisation (no omnigraph binary required)."""

import pytest

from omnigraph_memory import repo


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:mitodl/ol-django.git", "https://github.com/mitodl/ol-django"),
        ("https://github.com/mitodl/ol-django", "https://github.com/mitodl/ol-django"),
        (
            "https://github.com/mitodl/ol-django.git",
            "https://github.com/mitodl/ol-django",
        ),
        ("git@gitlab.com:grp/sub/repo.git", "https://gitlab.com/grp/sub/repo"),
        (
            "https://x-token@github.com/mitodl/repo.git",
            "https://github.com/mitodl/repo",
        ),
        ("ssh://git@github.com/mitodl/repo.git", "https://github.com/mitodl/repo"),
    ],
)
def test_normalise_to_https_uri(url, expected):
    assert repo._normalise(url) == expected


def test_detect_override_wins(monkeypatch):
    monkeypatch.setenv("OMNIGRAPH_MEMORY_REPO", "https://env/repo")
    assert repo.detect(override="https://override/repo") == "https://override/repo"


def test_detect_env_fallback(monkeypatch):
    monkeypatch.setenv("OMNIGRAPH_MEMORY_REPO", "https://env/repo")
    assert repo.detect() == "https://env/repo"
