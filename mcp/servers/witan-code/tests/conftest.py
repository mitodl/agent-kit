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


@pytest.fixture
def sample_repo(tmp_path, monkeypatch):
    """A tiny source tree plus a configured, isolated code store."""
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/cg")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))

    src = tmp_path / "src"
    src.mkdir()
    (src / "svc.py").write_text(SAMPLE)
    return src
