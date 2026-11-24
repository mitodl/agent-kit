"""Drive Neovim, eglot and lsp-mode against ``witan lsp`` with the guide's config.

Each editor runs headless, loads the configuration blocks extracted from
``docs/editors.md`` at test time (so what is checked is what the guide says),
and exercises the features the guide's "What each editor shows" table claims
against a freshly seeded local store. The drivers next to this file record
what they saw; the assertions are here.

Opt-in, because each editor takes a minute or more: set ``WITAN_EDITOR_CHECKS=1``.
With it set, a missing editor is a failure rather than a skip, so the workflow
that runs these (``.github/workflows/witan-editors.yml``) cannot go green by
losing its install step. The lsp-mode check also needs ``LSP_MODE_DIR``, a
package directory ``install_lsp_mode.el`` filled from MELPA.

The server is whatever ``witan`` and ``witan-code`` sit next to the running
interpreter, so run this under ``ol-agent-kit``, which pulls in both with their
``lsp`` extras::

    WITAN_EDITOR_CHECKS=1 LSP_MODE_DIR=... uv run --frozen --isolated \\
        --package ol-agent-kit --group test pytest mcp/servers/witan/tests/editors

Hermetic the way ``testsupport/hermetic.py`` is, which has already redirected
this process: every editor and server runs with HOME, the XDG dirs and every
witan path inside the test's own directory, no ambient ``WITAN_*`` or agent
session variables, a PATH with nothing from the real home on it, and a stub in
place of the agent a launch starts. Nothing talks to a deployment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest
from testsupport.hermetic import REAL_HOME

HERE = Path(__file__).resolve().parent
GUIDE = HERE.parents[1] / "docs" / "editors.md"
ENABLE_VAR = "WITAN_EDITOR_CHECKS"
DRIVER_TIMEOUT_SECONDS = 600
SMALL_TIMEOUT_SECONDS = 60
APP = "https://github.com/test/app"

pytestmark = pytest.mark.skipif(
    os.environ.get(ENABLE_VAR) != "1",
    reason=f"editor checks are opt-in: set {ENABLE_VAR}=1",
)

# (heading the block sits under, fence language) -> the file it is loaded as.
SNIPPETS = {
    ("Neovim", "lua"): "witan.lua",
    ("Emacs", "elisp"): "witan-core.el",
    ("eglot", "elisp"): "witan-eglot.el",
    ("lsp-mode", "elisp"): "witan-lsp-mode.el",
}

# Agent-session variables an editor started inside an agent inherits. The
# launch spec's unset_env must remove the first two from the agent's
# environment; the rest are cleared so the run does not depend on the caller.
AGENT_VARS = (
    "CLAUDE_SESSION_ID",
    "CLAUDE_PROJECT_DIR",
    "PI_SESSION_ID",
    "CODEX_SESSION_ID",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "SENTRY_DSN",
    "VIRTUAL_ENV",
)

# The agent a launch spec names. It records how it was started, as JSON, at
# the path the driver puts in STUB_OUT.
STUB_AGENT = """#!{python} -I
import json, os, sys
with open(os.environ["STUB_OUT"], "w") as f:
    json.dump({{"cwd": os.getcwd(), "argv": sys.argv[1:],
               "env": {{n: os.environ.get(n) for n in {names!r}}}}}, f)
print("stub agent ran")
"""
STUB_ENV_NAMES = ["CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR", "EDITOR_MARK"]


def editor(name: str) -> str:
    """The editor binary on the PATH pytest was started with.

    Resolved before the PATH is scrubbed of the real home, since a Nix or
    ``~/.local`` install lives there.
    """
    found = shutil.which(name)
    if found is None:
        pytest.fail(f"{name} is not on PATH, and {ENABLE_VAR}=1 asks for it")
    return found


def guide_snippets() -> dict[str, str]:
    """Each config block in the guide, by the file name it is loaded as.

    Matched by the heading it sits under and its fence language, and exactly
    one of each, so a restructured guide fails here rather than loading the
    wrong block.
    """
    found: dict[str, list[str]] = {name: [] for name in SNIPPETS.values()}
    heading = None
    pattern = re.compile(
        r"^#+ (?P<heading>.+?)\s*$|^```(?P<lang>\w*)\n(?P<body>.*?)^```$",
        re.MULTILINE | re.DOTALL,
    )
    for m in pattern.finditer(GUIDE.read_text()):
        if m["heading"]:
            heading = m["heading"]
        elif (name := SNIPPETS.get((heading, m["lang"]))) is not None:
            found[name].append(m["body"])
    counts = {name: len(bodies) for name, bodies in found.items()}
    assert counts == dict.fromkeys(SNIPPETS.values(), 1), (
        f"{GUIDE.name} must hold exactly one block of each (heading, fence language) "
        f"in {list(SNIPPETS)}; found {counts}"
    )
    return {name: bodies[0] for name, bodies in found.items()}


@pytest.fixture(scope="module")
def snippets(tmp_path_factory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("guide")
    paths = {}
    for name, body in guide_snippets().items():
        paths[name] = out / name
        paths[name].write_text(body)
    return paths


def editor_env(root: Path) -> dict[str, str]:
    """The environment every editor, server and seeding command runs in."""
    home = root / "home"
    bin_dir = root / "bin"
    for d in (home / ".config" / "witan", home / ".local" / "share", bin_dir):
        d.mkdir(parents=True, exist_ok=True)

    omnigraph = shutil.which("omnigraph")
    assert omnigraph, "omnigraph is not on PATH"
    (bin_dir / "omnigraph").symlink_to(omnigraph)
    agent = bin_dir / "claude"
    agent.write_text(STUB_AGENT.format(python=sys.executable, names=STUB_ENV_NAMES))
    agent.chmod(0o755)

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("WITAN_", "OTEL_")) and k not in AGENT_VARS
    }
    # Nothing from the real home: a real `claude` there must not be what a
    # launch finds. bin_dir holds the stub and the omnigraph link; the server's
    # venv is the running interpreter's.
    system_path = [
        d
        for d in os.environ["PATH"].split(os.pathsep)
        if d and not Path(d).resolve().is_relative_to(REAL_HOME)
    ]
    env.update(
        PATH=os.pathsep.join(
            [str(bin_dir), str(Path(sys.executable).parent), *system_path]
        ),
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        WITAN_CONFIG=str(home / ".config" / "witan" / "config.toml"),
        WITAN_TOKEN_CACHE=str(home / ".config" / "witan" / "tokens.json"),
        WITAN_MERGE_WATERMARKS=str(
            home / ".config" / "witan" / "merge-watermarks.json"
        ),
        WITAN_MEMORY_URI=str(root / "graph.omni"),
        WITAN_CODE_DIR=str(root / "code"),
        WITAN_AUTHOR="editor-check@example.invalid",
        # Seeded tasks carry github.com repo URLs; never look them up.
        WITAN_LIVE_PR="0",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    return env


@dataclass
class Store:
    root: Path
    env: dict[str, str]
    task: str
    task2: str
    stale_memory: str
    project: str

    @property
    def app(self) -> Path:
        return self.root / "repos" / "app"

    def stub_run(self, name: str) -> dict:
        """How the stub agent was started by the launch recorded as ``name``."""
        return json.loads((self.root / f"stub-{name}.json").read_text())


@pytest.fixture
def store(tmp_path) -> Store:
    env = editor_env(tmp_path)
    seeded = subprocess.run(
        [sys.executable, "-I", str(HERE / "seed.py"), str(tmp_path / "repos")],
        env=env,
        capture_output=True,
        text=True,
        timeout=DRIVER_TIMEOUT_SECONDS,
        check=False,
    )
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    return Store(root=tmp_path, env=env, **json.loads(seeded.stdout.splitlines()[-1]))


def run_driver(store: Store, argv: list[str], **extra: str) -> dict:
    """Run one editor driver to completion and return what it recorded."""
    result = store.root / "result.json"
    log = store.root / "editor.log"
    env = {
        **store.env,
        "ROOT": str(store.root),
        "RESULT": str(result),
        "TASK": store.task,
        "TASK2": store.task2,
        "PROJECT": store.project,
        # As if the editor had been started from inside an agent session, so
        # the launch spec's unset_env has something real to remove.
        "CLAUDE_SESSION_ID": "leaked-outer-session",
        "CLAUDE_PROJECT_DIR": "/nonexistent/outer-project",
        **extra,
    }
    with log.open("w") as out:
        proc = subprocess.run(
            argv,
            env=env,
            cwd=store.app,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=DRIVER_TIMEOUT_SECONDS,
            check=False,
        )
    recorded = json.loads(result.read_text()) if result.exists() else None
    # Shown by pytest only when an assertion below fails.
    print(f"exit {proc.returncode}\n--- editor log (tail)\n{log.read_text()[-4000:]}")
    print("--- recorded\n" + json.dumps(recorded, indent=2))
    assert proc.returncode == 0, "the editor exited non-zero; see its log above"
    assert recorded is not None, "the driver recorded nothing; see the editor log above"
    assert recorded.get("driver_error") is None, recorded["driver_error"]
    return recorded


def plain(text: str | None) -> str:
    """Hover text without markdown escapes, so a check holds for either form."""
    assert text, "no hover"
    return text.replace("\\", "")


def check_stub_runs(store: Store) -> None:
    """The task and project launches started the agent per the launch spec."""
    for name in ("task", "project"):
        run = store.stub_run(name)
        assert Path(run["cwd"]) == store.app, name
        assert run["env"] == {
            "CLAUDE_SESSION_ID": None,
            "CLAUDE_PROJECT_DIR": None,
            "EDITOR_MARK": "kept",
        }, name


def check_hovers(r: dict, store: Store) -> None:
    hover_helper = plain(r["hover_helper"])
    assert store.task in hover_helper
    assert "helper wraps compute" in hover_helper
    assert "lib/maths.py" in plain(r["hover_compute_unopened"])
    hover_env_var = plain(r["hover_env_var"])
    assert "MITOL_APP_BASE_URL" in hover_env_var
    assert "Pulumi.QA.yaml" in hover_env_var


def emacs_rows(rows: dict | None) -> list[str]:
    """Row texts of a list buffer, which the Emacs drivers record keyed by text."""
    return list(rows or {})


def check_emacs(r: dict, store: Store) -> None:
    """What eglot and lsp-mode share: the guide's witan-core.el at work."""
    assert r["attached"] is True
    assert any(store.stale_memory in d for d in r["diagnostics_service"])
    check_hovers(r, store)
    assert any(
        f"{store.app}/app/service.py:{n}" == ref
        for n in (5, 9)
        for ref in r["references_helper"]
    )
    assert len(r["references_helper"]) == 2
    assert emacs_rows(r["impact_list"]) == ["provider of env_var MITOL_APP_BASE_URL"]
    ready = emacs_rows(r["list_ready"])
    assert any(store.task in row for row in ready) and any(
        store.task2 in row for row in ready
    )
    assert [store.project in row for row in emacs_rows(r["list_projects"])] == [True]
    stale = r["list_stale_refs"]
    assert [store.stale_memory in row for row in stale] == [True]
    # RET on a stale-ref row visits the file the server named.
    assert next(iter(stale.values()))[1] == (store.app / "app" / "service.py").as_uri()
    assert r["run_task_stub_ran"] is True
    assert r["run_project_stub_ran"] is True
    assert [
        f"{store.task} (in_progress)" in row
        for row in emacs_rows(r["list_mywork_after_launch"])
    ] == [True]
    assert "launch spec version 2" in r["version_refused"]
    assert f"released {store.task2}" in r["failed_launch"]
    assert not any(
        store.task2 in row for row in emacs_rows(r["list_mywork_after_release"])
    )
    assert "tk-does-not-exist not launched" in r["refused_launch"]
    assert len(r["diagnostics_unindexed"]) == 1
    assert "has no code index" in r["diagnostics_unindexed"][0]
    check_stub_runs(store)


def test_neovim(store: Store, snippets: dict[str, Path]) -> None:
    r = run_driver(
        store,
        [
            editor("nvim"),
            "--headless",
            "--clean",
            "-u",
            "NONE",
            "-c",
            f"luafile {HERE / 'nvim_driver.lua'}",
        ],
        WITAN_LUA=str(snippets["witan.lua"]),
    )
    service = str(store.app / "app" / "service.py")
    maths = str(store.root / "repos" / "lib" / "lib" / "maths.py")

    def texts(items: list[dict]) -> list[str]:
        return [i["text"] for i in items]

    assert r["attached"] is True
    assert r["server_caps"]["codeLens"] is True
    assert {
        "witan.listCallers",
        "witan.listCrossRepoImpact",
        "witan.listReadyTasks",
        "witan.runTask",
        "witan.releaseTask",
    } <= set(r["server_caps"]["commands"])
    assert any(store.stale_memory in d for d in r["diagnostics_service"])
    check_hovers(r, store)
    assert [(i["file"], i["lnum"]) for i in r["lens_callers_qf"]] == [(service, 5)]
    assert texts(r["lens_callers_qf"])[0].startswith("Service.run")
    assert r["definition_jump"]["file"] == maths
    assert sorted((i["file"], i["lnum"]) for i in r["references_compute"]) == [
        (maths, 1),
        (maths, 5),
    ]
    assert sorted((i["file"], i["lnum"]) for i in r["references_helper"]) == [
        (service, 5),
        (service, 9),
    ]
    assert [
        t.startswith("provider of env_var MITOL_APP_BASE_URL")
        for t in texts(r["lens_impact_qf"])
    ] == [True]
    ready = texts(r["list_ready"])
    assert any(store.task in t for t in ready) and any(store.task2 in t for t in ready)
    assert [store.project in t for t in texts(r["list_projects"])] == [True]
    assert r["list_my_work"] == []
    assert [
        (store.stale_memory in i["text"], i["file"]) for i in r["list_stale_refs"]
    ] == [(True, service)]
    assert r["run_task_stub_ran"] is True
    assert r["run_task_terminal"] == "terminal"
    assert [
        f"{store.task} (in_progress)" in t for t in texts(r["list_mywork_after_launch"])
    ] == [True]
    assert r["run_project_stub_ran"] is True
    refused = r["version_refused"]
    assert [("launch spec version 2" in n) for n in refused["notes"]] == [True]
    assert refused["tabs_opened"] == 0 and refused["stub_ran"] is False
    assert any(f"released {store.task2}" in n for n in r["failed_launch_notes"])
    assert any("tk-does-not-exist not launched" in n for n in r["refused_launch_notes"])
    assert [
        (lnum, "has no code index" in msg)
        for lnum, _, msg in r["diagnostics_unindexed"]
    ] == [(0, True)]
    # A second git root gets its own client, and commands from it (and from the
    # quickfix window, with the cwd there) go to that client.
    assert r["clients_after_stray"] == 2
    assert r["stray_ready"] == {"title": "witan.listReadyTasks", "notes": []}
    assert r["quickfix_cwd_ready"] == {
        "buftype": "quickfix",
        "title": "witan.listReadyTasks",
        "notes": [],
    }
    check_stub_runs(store)


def test_eglot(store: Store, snippets: dict[str, Path]) -> None:
    r = run_driver(
        store,
        [editor("emacs"), "--batch", "-Q", "-l", str(HERE / "eglot_driver.el")],
        WITAN_CORE_EL=str(snippets["witan-core.el"]),
        WITAN_GLUE_EL=str(snippets["witan-eglot.el"]),
    )
    assert r["class"] == "witan-eglot-server"
    # eglot has no CodeLens: the lens is a code action, and running it lists
    # the rows.
    assert r["code_actions_helper"] == ["1 caller"]
    assert emacs_rows(r["code_action_list"]) == ["Service.run"]
    assert any("cross-repo" in a for a in r["code_actions_base_url"])
    # One server per project, so lib's definition is named in the hover only.
    assert r["eglot_lib_servers"] == 2
    assert r["run_task_buffer"] == f"*witan {store.task}*"
    check_emacs(r, store)


def test_lsp_mode(store: Store, snippets: dict[str, Path]) -> None:
    r = run_driver(
        store,
        [editor("emacs"), "--batch", "-Q", "-l", str(HERE / "lsp_mode_driver.el")],
        WITAN_CORE_EL=str(snippets["witan-core.el"]),
        WITAN_GLUE_EL=str(snippets["witan-lsp-mode.el"]),
        LSP_MODE_DIR=os.environ["LSP_MODE_DIR"],
    )
    assert r["servers"] == ["witan"]
    assert " 1 caller" in r["lens_overlays"]
    assert r["lens_run_log"] == ["1 caller"]
    assert emacs_rows(r["lens_callers_list"]) == ["Service.run"]
    # lib joins the same multi-root server, so its definition opens as a file.
    assert r["definition_opened"] == [f"{store.root}/repos/lib/lib/maths.py:1"]
    check_emacs(r, store)


def test_neovim_routes_commands_to_innermost_folder(
    snippets: dict[str, Path], tmp_path
) -> None:
    """A nested checkout's command goes to its own client, not its parent's."""
    proc = subprocess.run(
        [
            editor("nvim"),
            "--headless",
            "--clean",
            "-u",
            "NONE",
            "-l",
            str(HERE / "nvim_routing.lua"),
        ],
        env={
            "T": str(tmp_path),
            "WITAN_LUA": str(snippets["witan.lua"]),
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=SMALL_TIMEOUT_SECONDS,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == [
        "inner witan.listReadyTasks",
        "outer witan.listReadyTasks",
        # A sibling sharing the inner folder's name as a prefix is not inside it.
        "outer witan.listReadyTasks",
    ]


def test_emacs_uris_and_terminal_names(snippets: dict[str, Path], tmp_path) -> None:
    """witan-core.el's URI encoding, list visits and terminal buffer names."""
    utf8_file = tmp_path / "dír é" / "f.py"
    utf8_file.parent.mkdir()
    utf8_file.write_text("x = 1\n")
    proc = subprocess.run(
        [
            editor("emacs"),
            "--batch",
            "-Q",
            "-l",
            str(snippets["witan-core.el"]),
            "-l",
            str(HERE / "emacs_uris.el"),
        ],
        env={
            "T": str(tmp_path),
            "UTF8_URI": utf8_file.as_uri(),
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "SHELL": "/bin/sh",
            "TERM": "dumb",
        },
        capture_output=True,
        text=True,
        timeout=SMALL_TIMEOUT_SECONDS,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    # The server decodes each URI the way pygls does; it must get the path back.
    assert [url2pathname(urlsplit(u).path) for u in out["uris"]] == out["paths"]
    assert out["visited"] == str(utf8_file)
    assert sorted(name for name, _ in out["terminals"]) == [
        "*witan wp-x*",
        "*witan wp-x<2>*",
    ]
    assert all(alive for _, alive in out["terminals"])
