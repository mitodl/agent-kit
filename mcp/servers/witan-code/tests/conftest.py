"""Shared fixtures for witan-code tests.

Tests index a small source tree into a throwaway per-repo store and exercise the
real omnigraph queries (including edge traversal) and the tree-sitter indexer.
Skipped when the omnigraph binary is unavailable. Tree-sitter grammars are
always available — they're pinned project dependencies (individual
tree-sitter-<lang> wheels, not the optional tree-sitter-language-pack), not an
optional extra — so there's nothing to gate on there.
"""

import shutil

import pytest

omnigraph_available = shutil.which("omnigraph") is not None

requires_stack = pytest.mark.skipif(
    not omnigraph_available,
    reason="requires omnigraph binary",
)

# Narrower than requires_stack: maintenance (optimize/cleanup) exercises the
# omnigraph binary directly and has no tree-sitter involvement.
requires_omnigraph = pytest.mark.skipif(
    not omnigraph_available, reason="requires omnigraph binary"
)

SAMPLE = """\
class Service:
    def run(self):
        return helper()


def helper():
    return 1


def main():
    s = Service()
    return s.run()
"""


@pytest.fixture(autouse=True)
def _fresh_identity():
    """Forget the process-lifetime actor between tests.

    ``identity.actor_id`` memoizes deliberately (a witan-code process writes as
    exactly one identity), which without this would let the first test that
    resolves one decide the branch-view names for every test after it.
    """
    from witan_code import identity

    identity.reset_cache()
    yield
    identity.reset_cache()


@pytest.fixture(autouse=True)
def _fresh_git_context():
    """Forget the memoized git context between tests.

    ``server._git_context`` caches ``detect()``/``store_branch()`` for
    ``_GIT_CONTEXT_TTL`` (2s) to amortize git subprocesses across one agent
    turn. Across tests that TTL is a *race*: a test that sets ``WITAN_REPO``
    within 2s of an earlier one gets the earlier test's repo, and its own
    ``monkeypatch.setenv`` is silently ignored. Whether that lands depends on
    how fast the preceding tests ran, which is why it reproduced locally and
    not in CI.
    """
    from witan_code import server

    server._git_context.clear()
    yield
    server._git_context.clear()


@pytest.fixture
def sample_repo(tmp_path, monkeypatch):
    """A tiny source tree plus a configured, isolated code store."""
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))

    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text(SAMPLE)
    return src
