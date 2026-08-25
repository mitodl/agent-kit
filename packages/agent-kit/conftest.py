"""Load the workspace's shared test-environment guard.

This file exists to be imported EARLY. ``testsupport.hermetic`` redirects every
ambient input — HOME, the XDG dirs, the witan state files and graph stores, the
terminal width — at import time rather than in a fixture, because the leak it
exists to stop can happen while pytest is still collecting (importing
``witan.server`` creates a graph). A rootdir ``conftest.py`` is the earliest
hook that runs for every invocation, `just test-*` or a bare ``pytest`` alike.

``pytest_plugins`` is only honoured in a rootdir conftest, which is the other
reason this is here and not in ``tests/conftest.py``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest_plugins = ["testsupport.hermetic"]
