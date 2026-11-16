"""Cluster graph ids for the code graphs on a shared omnigraph-server.

Each repo's code graph is a distinct cluster graph addressed as
``--server <url> --graph <id>``. Two sides have to agree on that id
byte-for-byte: witan-code derives it from a checkout's repo key to pick its
``--graph``, and ol-infrastructure's omnigraph stack derives it from
``managed_repos`` to declare the graph in ``cluster.yaml``. When they disagree,
a client addresses a graph the cluster never created. Both import
:func:`code_graph_id` from here, so there is one implementation to change, and
``tests/test_graph_ids.py`` pins its output. Changing that output renames every
deployed code graph, which needs a migration on the cluster, not only a
release.

Stdlib-only, like :mod:`witan_core.repo_key`, so the Pulumi program can depend
on witan-core without pulling in either server's dependencies.
"""

from __future__ import annotations

import hashlib
import re

CODE_GRAPH_PREFIX = "code-"
# The shared cross-repo bridge graph (Layer 2.5), the deployed analogue of
# witan-code's local `_bridge.omni` store. Fixed, not derived from any repo.
BRIDGE_GRAPH_ID = "code-bridge"
# omnigraph graph ids: letters, digits, hyphens; 1-64 chars. No underscores
# (the engine reserves them) and no path separators.
GRAPH_ID_MAX_LEN = 64
GRAPH_ID_RE = re.compile(r"^[a-zA-Z0-9-]{1,64}$")
_HASH_LEN = 8


def code_graph_id(repo: str) -> str:
    """Return the cluster graph id for ``repo``'s code graph.

    e.g. ``https://github.com/mitodl/ol-django`` ->
    ``code-github-com-mitodl-ol-django``.

    Strips the URI scheme, collapses every run of non-alphanumerics to ``-``,
    lowercases, and prefixes :data:`CODE_GRAPH_PREFIX`. An id longer than
    :data:`GRAPH_ID_MAX_LEN` is truncated and suffixed with a hash of the full
    repo URI, so two long repos sharing a prefix still get distinct ids. The
    result always matches :data:`GRAPH_ID_RE`.

    :param repo: The canonical repo URI (see :func:`witan_core.repo_key.normalise`).
    :returns: The graph id.
    :rtype: str
    """
    body = re.sub(r"(?i)^[a-z][a-z0-9+.-]*://", "", repo)
    body = re.sub(r"[^a-zA-Z0-9]+", "-", body).strip("-").lower()
    candidate = f"{CODE_GRAPH_PREFIX}{body}"
    if len(candidate) <= GRAPH_ID_MAX_LEN:
        return candidate
    digest = hashlib.sha256(repo.encode()).hexdigest()[:_HASH_LEN]
    keep = GRAPH_ID_MAX_LEN - len(CODE_GRAPH_PREFIX) - _HASH_LEN - 1
    return f"{CODE_GRAPH_PREFIX}{body[:keep].strip('-')}-{digest}"
