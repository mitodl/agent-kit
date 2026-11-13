"""Golden contract test for the cluster code-graph id.

witan-code picks ``--graph`` with ``code_graph_id`` and ol-infrastructure's
omnigraph stack declares the same id in ``cluster.yaml`` with the same import.
Every expected value below names a graph that exists (or would exist) on a
deployed cluster, so a change here renames graphs out from under their data.
Pin the observable mapping, including the hashed long-id form.
"""

import pytest

from witan_core.graph_ids import (
    BRIDGE_GRAPH_ID,
    CODE_GRAPH_PREFIX,
    GRAPH_ID_MAX_LEN,
    GRAPH_ID_RE,
    code_graph_id,
)

_LONG = "https://github.com/mitodl/" + "a" * 200


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("https://github.com/mitodl/ol-django", "code-github-com-mitodl-ol-django"),
        ("https://github.com/mitodl/agent-kit", "code-github-com-mitodl-agent-kit"),
        (
            "https://github.com/mitodl/ol-infrastructure",
            "code-github-com-mitodl-ol-infrastructure",
        ),
        # scheme stripped
        (
            "github.com/mitodl/ol-infrastructure",
            "code-github-com-mitodl-ol-infrastructure",
        ),
        ("http://example.com/x", "code-example-com-x"),
        # underscores and dots collapse, case folds
        (
            "https://github.com/MITODL/Mixed_Case.Repo",
            "code-github-com-mitodl-mixed-case-repo",
        ),
        # multi-segment paths keep every segment
        ("https://gitlab.com/grp/sub/repo", "code-gitlab-com-grp-sub-repo"),
        # not a canonical repo key, but the function must still be total
        (
            "git@github.com:mitodl/agent-kit.git",
            "code-git-github-com-mitodl-agent-kit-git",
        ),
        # over 64 chars: truncated, then a hash of the full URI
        (
            "https://github.com/mitodl/open-edx-plugins-with-a-very-long-repository-name",
            "code-github-com-mitodl-open-edx-plugins-with-a-very-lon-78dc330d",
        ),
        (_LONG, "code-github-com-mitodl-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-323a4342"),
        (
            _LONG[:-1] + "b",
            "code-github-com-mitodl-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-ca584501",
        ),
    ],
)
def test_code_graph_id_golden(repo, expected):
    assert code_graph_id(repo) == expected


@pytest.mark.parametrize(
    "repo",
    [
        "https://github.com/mitodl/ol-django",
        "https://github.com/MITODL/Mixed_Case.Repo",
        _LONG,
        "https://github.com/mitodl/" + "x_" * 80,
    ],
)
def test_code_graph_id_is_a_valid_prefixed_graph_id(repo):
    gid = code_graph_id(repo)
    assert GRAPH_ID_RE.match(gid)
    assert len(gid) <= GRAPH_ID_MAX_LEN
    assert gid.startswith(CODE_GRAPH_PREFIX)


def test_bridge_graph_id():
    assert BRIDGE_GRAPH_ID == "code-bridge"
    assert GRAPH_ID_RE.match(BRIDGE_GRAPH_ID)
