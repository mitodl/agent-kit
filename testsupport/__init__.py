"""Test-support shared across the workspace's packages.

Not a distributed package and deliberately not one: it is imported by each
package's rootdir ``conftest.py`` off the repo root, so it needs no release,
no version, and no place in ``just check-versions``. See ``hermetic``.
"""
