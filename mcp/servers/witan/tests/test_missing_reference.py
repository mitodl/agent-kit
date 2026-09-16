"""A write naming a slug that does not exist is refused by name.

WITAN-6 and WITAN-V: ``task_create`` with a placeholder blocker slug and
``workflow_session_start`` with a mistyped project reached the store and came
back as "omnigraph mutate failed" with the engine's error report.
"""

import pytest
from fastmcp.exceptions import ToolError

from witan import server as srv
from witan.scan import WriteBlocked

from .conftest import requires_omnigraph

MISSING_TASK = "tk-wire-ci-and-qa-applications-PLACEHOLDER"
MISSING_PROJECT = "wp-postpublish-podhome-driven-post-publication-pipe-ade3c5"


@requires_omnigraph
@pytest.mark.parametrize(
    ("kwargs", "node_type", "slug"),
    [
        ({"project_slug": MISSING_PROJECT}, "WorkflowProject", MISSING_PROJECT),
        ({"parent": MISSING_TASK}, "Task", MISSING_TASK),
        ({"blocked_by": [MISSING_TASK]}, "Task", MISSING_TASK),
        ({"discovered_from": [MISSING_TASK]}, "Task", MISSING_TASK),
    ],
)
def test_task_create_names_the_missing_slug(server, kwargs, node_type, slug):
    with pytest.raises(srv.MissingReference) as excinfo:
        server.task_create("t", "d", **kwargs)

    assert str(excinfo.value) == f"task_create: no {node_type} with slug '{slug}'"
    # The batch commits whole, so the task node did not land without its edge.
    assert server.task_list(repo="") == []


@requires_omnigraph
def test_session_start_names_the_missing_project(server, tmp_state_dir):
    with pytest.raises(srv.MissingReference) as excinfo:
        server.workflow_session_start(MISSING_PROJECT, "sid", "implementation")

    assert excinfo.value.node_type == "WorkflowProject"
    assert excinfo.value.slug == MISSING_PROJECT


@requires_omnigraph
def test_task_link_names_the_missing_blocker(server):
    blocked = server.task_create("blocked", "d")["slug"]

    with pytest.raises(srv.MissingReference, match=MISSING_TASK):
        server.task_link(MISSING_TASK, blocked, "blocks")

    assert server.task_get(blocked)["blocked_by"] is None


def test_refusal_is_a_tool_error():
    # A ToolError is logged at its own level without a traceback, rather than
    # through logger.exception into Sentry.
    assert issubclass(srv.MissingReference, ToolError)


def test_http_transport_message_is_translated():
    message = (
        "omnigraph mutate failed: src 'tk-x' not found in Task (HTTP 400, bad_request)"
    )

    with pytest.raises(srv.MissingReference) as excinfo:
        with srv._missing_endpoint_errors("task_create"):
            raise RuntimeError(message)

    assert (excinfo.value.node_type, excinfo.value.slug) == ("Task", "tk-x")


def test_other_failures_pass_through():
    with pytest.raises(RuntimeError, match="^omnigraph mutate failed: boom$"):
        with srv._missing_endpoint_errors("task_create"):
            raise RuntimeError("omnigraph mutate failed: boom")


def test_a_write_blocked_refusal_is_not_rewritten():
    exc = WriteBlocked("insert_task", [])
    exc.args = ("dst 'tk-x' not found in Task",)

    with pytest.raises(WriteBlocked):
        with srv._missing_endpoint_errors("task_create"):
            raise exc
