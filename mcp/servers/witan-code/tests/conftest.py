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


# Every input `identity._resolve` reads. Isolating the file paths is not
# enough on its own: each of these env vars short-circuits ahead of them.
_IDENTITY_ENV = (
    "WITAN_ACTOR",
    "WITAN_REMOTE_URL",
    "WITAN_OIDC_ISSUER",
    "WITAN_TARGET",
)


@pytest.fixture(autouse=True)
def _fresh_identity(tmp_path, monkeypatch):
    """Pin the identity these tests run under: nobody, unless one is asked for.

    Two jobs. The first is to forget the process-lifetime actor between tests —
    ``identity.actor_id`` memoizes deliberately (a witan-code process writes as
    exactly one identity), which without this would let the first test that
    resolves one decide the branch-view names for every test after it.

    The second is to stop that resolution reaching the machine. It reads the
    real ``~/.config/witan/config.toml`` and the real OIDC token cache, so on a
    developer's box — logged in, unlike a CI runner — ``actor_id()`` returns an
    ``act-…`` and the branch views get namespaced under it. Four tests asserted
    un-namespaced names and failed for everyone who had run ``witan login``,
    green in CI the whole time. Pointing both at ``tmp_path`` makes logged-out
    the deterministic default everywhere; ``logged_in_actor`` opts back in.
    """
    from witan_code import identity

    monkeypatch.setenv("WITAN_CONFIG", str(tmp_path / "witan-config.toml"))
    monkeypatch.setenv("WITAN_TOKEN_CACHE", str(tmp_path / "witan-tokens.json"))
    for var in _IDENTITY_ENV:
        monkeypatch.delenv(var, raising=False)

    identity.reset_cache()
    yield
    identity.reset_cache()


@pytest.fixture
def logged_in_actor(monkeypatch):
    """Run the test as a specific actor — the half of the guard CI never sees.

    Set through ``WITAN_ACTOR`` rather than by stubbing ``actor_id``, so the
    resolution path a non-interactive writer (the CI indexer, a maintenance
    job) actually takes is the one under test.
    """
    from witan_code import identity

    def _login(actor: str = "act-alice") -> str:
        monkeypatch.setenv(identity.ACTOR_ENV_VAR, actor)
        identity.reset_cache()
        return actor

    return _login


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
