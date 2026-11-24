"""Build the store the editor drivers run against, and print its slugs as JSON.

Run as a subprocess with the editor environment (``test_editors.editor_env``),
never imported: importing ``witan.server`` binds the graph named by
``WITAN_MEMORY_URI`` at that moment, and the pytest process's own value is the
session-wide fake home rather than this test's store.

Three indexed checkouts (``app`` calls into ``lib``, ``infra`` provides the env
var ``app`` reads), one unindexed checkout (``stray``), two ready tasks, a
project, a memory on ``helper`` and a memory naming a symbol that no longer
exists.

:param argv[1]: the directory to build the checkouts in.
"""

import asyncio
import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path

APP = "https://github.com/test/app"

FILES = {
    "app": {
        "app/service.py": """\
from lib.maths import compute


class Service:
    def run(self):
        return helper()


def helper():
    return compute()


def main():
    s = Service()
    return s.run()
""",
        "app/settings.py": """\
import os


def base_url():
    return os.environ["MITOL_APP_BASE_URL"]
""",
    },
    "infra": {
        "Pulumi.QA.yaml": """\
config:
  app:env_vars:
    MITOL_APP_BASE_URL: "https://app-qa.example.org"
""",
    },
    "lib": {
        "lib/maths.py": """\
def compute():
    return 1


def twice():
    return compute() + compute()
""",
    },
    "stray": {"m.py": "x = 1\n"},
}
INDEXED = ("app", "infra", "lib")


def checkout(repos: Path, name: str) -> Path:
    path = repos / name
    for rel, text in FILES[name].items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    git = ["git", "-C", str(path)]
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        [*git, "remote", "add", "origin", f"https://github.com/test/{name}"], check=True
    )
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    return path


def call(tool, **kwargs):
    result = getattr(tool, "fn", tool)(**kwargs)
    return asyncio.run(result) if inspect.iscoroutine(result) else result


def main() -> None:
    repos = Path(sys.argv[1])
    witan_code = shutil.which("witan-code")
    assert witan_code, "witan-code is not on PATH"
    for name in FILES:
        path = checkout(repos, name)
        if name in INDEXED:
            subprocess.run(
                [witan_code, "index", str(path)], check=True, capture_output=True
            )

    from witan import server

    helper = f"{APP}#app/service.py::helper"
    task = call(
        server.task_create,
        title="Make helper configurable",
        description="helper should read its source from settings.",
        repo=APP,
        symbol_refs=[helper],
    )
    task2 = call(
        server.task_create, title="Second ready task", description="Filler.", repo=APP
    )
    call(
        server.memory_store,
        kind="pattern",
        title="helper wraps compute",
        content="helper() delegates to lib.maths.compute; change both together.",
        repo=APP,
        symbol_refs=[helper],
    )
    stale = call(
        server.memory_store,
        kind="lesson",
        title="Service.start was renamed",
        content="Service.start became Service.run; old callers break.",
        repo=APP,
        symbol_refs=[f"{APP}#app/service.py::start"],
    )
    project = call(
        server.workflow_project_create,
        title="Editor check project",
        description="Seeded project.",
        repos=[APP],
    )
    print(
        json.dumps(
            {
                "task": task["slug"],
                "task2": task2["slug"],
                "stale_memory": stale["slug"],
                "project": project["slug"],
            }
        )
    )


main()
