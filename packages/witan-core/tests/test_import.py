"""Package-level invariants.

The leaf-package invariant (importing witan_core must pull in neither server) is
load-bearing: it is what lets both servers depend on witan_core without a cycle.
"""

import sys


def test_public_surface_is_exported():
    import witan_core

    assert "popen_detached" in witan_core.__all__
    for name in witan_core.__all__:
        assert hasattr(witan_core, name), name


def test_no_cross_package_import():
    """witan_core must import neither witan nor witan_code (leaf invariant)."""
    import witan_core  # noqa: F401

    assert "witan" not in sys.modules
    assert "witan_code" not in sys.modules
