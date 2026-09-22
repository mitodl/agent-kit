"""Structured ``--output-format`` against a real store: empty lists and detail views.

Every list command gets its own empty case here rather than one standing in
for all of them, because the defect was per command: each had its own early
return that printed prose before the format was consulted.
"""

from __future__ import annotations

import json
import tomllib

import pytest
import yaml

from witan.cli import _common
from witan.cli import output as output_module
from witan.cli._common import _fn


@pytest.fixture(autouse=True)
def _reset_output_format():
    output_module.set_output_format("txt")
    yield
    output_module.set_output_format("txt")


@pytest.fixture
def cli(server, monkeypatch):
    monkeypatch.setattr(_common, "_server", server)
    return server


def _as(fmt: str) -> None:
    output_module.set_output_format(fmt)


def _project(server, title: str = "Empty project") -> str:
    return _fn(server.workflow_project_create)(title=title, description="d")["slug"]


def _call_tasks(server):
    from witan.cli.tasks import tasks

    tasks(all_repos=True)


def _call_tasks_repo_scoped(server):
    """The branch whose prose carried the --all-repos hint."""
    from witan.cli.tasks import tasks

    tasks()


def _call_tasks_query(server):
    from witan.cli.tasks import tasks

    tasks("nothing is called this", all_repos=True)


def _call_tasks_ready(server):
    from witan.cli.tasks import tasks

    tasks(ready=True, all_repos=True)


def _call_projects(server):
    from witan.cli.projects import projects

    projects(all_repos=True)


def _call_projects_query(server):
    from witan.cli.projects import projects

    projects("nothing is called this", all_repos=True)


def _call_memory(server):
    from witan.cli.memory import memory

    memory(all_repos=True)


def _call_memory_query(server):
    from witan.cli.memory import memory

    memory("nothing is called this", all_repos=True)


def _call_traces(server):
    from witan.cli.traces import traces

    traces(all_repos=True)


def _call_project_tasks(server):
    from witan.cli.projects import project_tasks

    project_tasks(_project(server))


def _call_session_list(server):
    from witan.cli.session import session_list

    session_list(_project(server))


EMPTY_LISTS = {
    "tasks": _call_tasks,
    "tasks-repo-scoped": _call_tasks_repo_scoped,
    "tasks-query": _call_tasks_query,
    "tasks-ready": _call_tasks_ready,
    "projects": _call_projects,
    "projects-query": _call_projects_query,
    "memory": _call_memory,
    "memory-query": _call_memory_query,
    "traces": _call_traces,
    "project-tasks": _call_project_tasks,
    "session-list": _call_session_list,
}


@pytest.mark.parametrize("call", EMPTY_LISTS.values(), ids=EMPTY_LISTS.keys())
def test_an_empty_list_is_empty_rows_not_prose(cli, capsys, call):
    _as("json")

    call(cli)

    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == []


@pytest.mark.parametrize("call", EMPTY_LISTS.values(), ids=EMPTY_LISTS.keys())
def test_an_empty_list_still_says_so_in_txt(cli, monkeypatch, call):
    printed = []
    monkeypatch.setattr(
        _common.console, "print", lambda *a, **kw: printed.append(str(a[0]))
    )

    call(cli)

    assert any("No " in line for line in printed), printed


def test_task_show_prints_task_get_record(cli, capsys):
    from witan.cli.tasks import _task_show

    blocker = _fn(cli.task_create)(title="upstream", description="d")
    task = _fn(cli.task_create)(
        title="the task", description="body", blocked_by=[blocker["slug"]]
    )
    _fn(cli.task_comment)(slug=task["slug"], text="a note")
    _as("json")

    _task_show(task["slug"])

    shown = json.loads(capsys.readouterr().out)
    assert shown == _fn(cli.task_get)(slug=task["slug"])
    assert shown["comments"][0]["body"] == "a note"
    assert shown["closed_at"] is None


def test_task_show_under_toml_omits_nulls_instead_of_blanking_them(cli, capsys):
    from witan.cli.tasks import _task_show

    task = _fn(cli.task_create)(title="the task", description="body")
    _as("toml")

    _task_show(task["slug"])

    shown = tomllib.loads(capsys.readouterr().out)
    record = _fn(cli.task_get)(slug=task["slug"])
    assert "closed_at" not in shown
    assert shown == {k: v for k, v in record.items() if v is not None}


def test_task_show_is_reachable_as_a_named_subcommand(cli, capsys):
    from witan.cli._common import app

    task = _fn(cli.task_create)(title="the task", description="body")
    _as("json")

    command, bound, _ = app.parse_args(["task", "show", task["slug"]])
    command(*bound.args, **bound.kwargs)

    assert json.loads(capsys.readouterr().out)["slug"] == task["slug"]


@pytest.mark.parametrize("fmt", ["txt", "json"])
def test_task_show_of_a_missing_task_exits_nonzero_with_nothing_on_stdout(
    cli, capsys, fmt
):
    from witan.cli.tasks import _task_show

    _as(fmt)

    with pytest.raises(SystemExit) as exc:
        _task_show("tk-does-not-exist")

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "tk-does-not-exist" in captured.err


def test_project_status_honours_the_global_format(cli, capsys):
    from witan.cli.projects import project_status

    slug = _project(cli)
    _as("json")

    project_status(slug)

    assert json.loads(capsys.readouterr().out) == _fn(cli.workflow_project_status)(
        slug=slug
    )


def test_project_status_global_format_wins_over_json_flag(cli, capsys):
    from witan.cli.projects import project_status

    slug = _project(cli)
    _as("yaml")

    project_status(slug, json=True)

    out = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert yaml.safe_load(out)["project"]["slug"] == slug


def test_project_status_json_flag_wins_over_txt(cli, capsys):
    """txt may be ambient (WITAN_OUTPUT_FORMAT), so the per-command flag wins."""
    from witan.cli.projects import project_status

    slug = _project(cli)
    _as("txt")

    project_status(slug, json=True)

    assert json.loads(capsys.readouterr().out)["project"]["slug"] == slug


def test_project_status_json_flag_alone_still_prints_json(cli, capsys):
    from witan.cli.projects import project_status

    slug = _project(cli)

    project_status(slug, json=True)

    assert json.loads(capsys.readouterr().out)["project"]["slug"] == slug


def test_project_tasks_detail_adds_dependents(cli, capsys):
    from witan.cli.projects import project_tasks

    slug = _project(cli)
    blocker = _fn(cli.task_create)(title="first", description="d", project_slug=slug)
    blocked = _fn(cli.task_create)(
        title="second",
        description="d",
        project_slug=slug,
        blocked_by=[blocker["slug"]],
    )
    _as("json")

    project_tasks(slug, detail=True)

    rows = {r["slug"]: r for r in json.loads(capsys.readouterr().out)["rows"]}
    assert rows[blocker["slug"]]["dependents"] == [blocked["slug"]]
    assert rows[blocked["slug"]]["dependents"] == []
    assert rows[blocked["slug"]]["blocked_by"] == [blocker["slug"]]


def test_project_show_of_a_missing_project_exits_nonzero_with_nothing_on_stdout(
    cli, capsys
):
    from witan.cli.projects import _project_show

    with pytest.raises(SystemExit) as exc:
        _project_show("wp-does-not-exist")

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "wp-does-not-exist" in captured.err
