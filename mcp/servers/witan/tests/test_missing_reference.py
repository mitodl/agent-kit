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


@requires_omnigraph
def test_task_link_names_a_missing_child(server):
    # The one path the engine never sees: `_update_task` finds no child row and
    # writes nothing, so there is no rejection to translate.
    parent = server.task_create("parent", "d")["slug"]

    with pytest.raises(srv.MissingReference, match=MISSING_TASK):
        server.task_link(parent, MISSING_TASK, "parent")


def test_refusal_is_a_tool_error():
    # A ToolError is logged at its own level without a traceback, rather than
    # through logger.exception into Sentry.
    assert issubclass(srv.MissingReference, ToolError)


@pytest.mark.parametrize(
    ("message", "node_type", "slug"),
    [
        (
            "omnigraph mutate failed: src 'tk-x' not found in Task "
            "(HTTP 400, bad_request)",
            "Task",
            "tk-x",
        ),
        (
            "omnigraph mutate failed (exit 1):\nError: \n"
            "   0: \x1b[91m__dst 'wp-x' not found in WorkflowProject\x1b[0m\n\n"
            "Location:\n   crates/omnigraph-cli/src/client.rs:867",
            "WorkflowProject",
            "wp-x",
        ),
    ],
    ids=["http", "local-cli"],
)
def test_engine_message_is_translated(message, node_type, slug):
    with pytest.raises(srv.MissingReference) as excinfo:
        with srv._missing_endpoint_errors("task_create"):
            raise RuntimeError(message)

    assert (excinfo.value.node_type, excinfo.value.slug) == (node_type, slug)


def test_a_chunked_batch_failure_is_translated_through_its_cause():
    engine = RuntimeError("omnigraph mutate failed: dst 'tk-x' not found in Task")

    with pytest.raises(srv.MissingReference, match="tk-x"):
        with srv._missing_endpoint_errors("task_create"):
            raise RuntimeError("chunk 2/3 of a 9-statement batch failed") from engine


def test_other_failures_pass_through():
    with pytest.raises(RuntimeError, match="^omnigraph mutate failed: boom$"):
        with srv._missing_endpoint_errors("task_create"):
            raise RuntimeError("omnigraph mutate failed: boom")


def test_caller_input_quoted_in_an_error_is_not_matched():
    # store_merge quotes a malformed row; the row's content is not the engine.
    message = "export.jsonl: export row is not a JSON object: \"dst 'tk-x' not found in Task\""

    with pytest.raises(RuntimeError, match="export row"):
        with srv._missing_endpoint_errors("store_merge"):
            raise RuntimeError(message)


def test_a_write_blocked_refusal_is_not_rewritten():
    exc = WriteBlocked("insert_task", [])
    exc.args = ("dst 'tk-x' not found in Task",)

    with pytest.raises(WriteBlocked):
        with srv._missing_endpoint_errors("task_create"):
            raise exc


def test_local_cli_prints_the_refusal_instead_of_a_traceback(monkeypatch):
    from witan import cli

    def refuse():
        raise srv.MissingReference("task_link", "Task", MISSING_TASK)

    printed = []
    # `App.meta` is a read-only property, so it is replaced on the class.
    monkeypatch.setattr(type(cli.app), "meta", property(lambda self: refuse))
    monkeypatch.setattr(
        cli.console, "print", lambda *a, **kw: printed.append(str(a[0]))
    )

    with pytest.raises(SystemExit) as exit_code:
        cli.main()

    assert exit_code.value.code == 1
    assert printed == [f"task_link: no Task with slug '{MISSING_TASK}'"]
