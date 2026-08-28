"""Load the workspace's shared test-environment guard.

This file exists to be imported EARLY. ``testsupport.hermetic`` redirects every
ambient input — HOME, the XDG dirs, the witan state files and graph stores, the
terminal width — at import time rather than in a fixture, because the leak it
exists to stop can happen while pytest is still collecting (importing
``witan.server`` creates a graph). A rootdir ``conftest.py`` is the earliest
hook that runs for every invocation.

★ IMPORTED, not named in ``pytest_plugins``. That setting is only honoured in
whichever conftest is TOP-LEVEL for the rootdir pytest picked, and the rootdir
depends on the arguments: run ``pytest`` from the repo root and all five of
these become non-top-level, aborting collection outright with "Defining
'pytest_plugins' in a non-top-level conftest is no longer supported". A plain
import carries no such rule, does the same redirection (it happens at module
import), and re-exporting the hook below makes this conftest its own plugin.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# The import is the point: `testsupport.hermetic` redirects the environment at
# module scope. The hook re-export is what lets this conftest report a leak.
from testsupport.hermetic import pytest_sessionfinish  # noqa: E402,F401
