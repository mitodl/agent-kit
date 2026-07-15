"""Smoke tests for the scaffolded package.

Until the extraction tasks land real modules here, these just prove the package
is importable and declares no public surface yet — and, crucially, that it pulls
in neither server (the leaf-package invariant).
"""

import sys


def test_witan_core_imports():
    import witan_core

    assert witan_core.__all__ == []


def test_no_cross_package_import():
    """witan_core must import neither witan nor witan_code (leaf invariant)."""
    import witan_core  # noqa: F401

    assert "witan" not in sys.modules
    assert "witan_code" not in sys.modules
