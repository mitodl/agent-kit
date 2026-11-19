"""The launch contract: what `witan task run` / `witan project run` decide to run.

ADR 0012 ("3. The launch contract") is the spec. These cover the rules a
client relies on without reading the code: the workspace check runs before the
claim, a launch claim is bare, a refusal carries no argv, a project launch
claims nothing, and `task_release`'s `if_claimed_at` fence.
"""

import json
from pathlib import Path

import pytest

from witan.session_state import SESSION_ID_ENV_VARS

from .conftest import requires_omnigraph

REPO = "https://github.com/test/repo"
OTHER_REPO = "https://github.com/test/other"


class _Cfg:
    agent = "claude"
    model = "opus"
    target_name = None


class _Fake:
    """The tools a launch calls, recording each claim and the env it saw."""

    def __init__(self, task=None, project=None, claim=None):
        self.task = task
        self.project = project
        self.claim = claim
        self.claims = []
        self.checkouts = []

    def task_get(self, slug):
        return self.task

    def workflow_project_get(self, slug):
        return self.project

    def task_claim(self, slug, force=False, repo=None, branch=None):
        import os

        self.claims.append({name: os.environ.get(name) for name in SESSION_ID_ENV_VARS})
        self.checkouts.append((repo, branch))
        return self.claim

    def task_ready(self, project_slug, limit):
        return [{"slug": "tk-r", "priority": "p1", "title": "ready one"}]

    def recall(self, **_kwargs):
        return {"memories": []}


_CLAIMED = {
    "slug": "tk-a",
    "claimed": True,
    "assignee": "pytest",
    "claimed_at": "2026-11-17T16:20:04.607+00:00",
    "overlaps": [],
    "warning": "Claimed as 'pytest', which names no session.",
}


def _task(repo=REPO):
    return {"slug": "tk-a", "title": "T", "type": "bug", "repo": repo}


@pytest.fixture(autouse=True)
def session_env(monkeypatch):
    """Start every test with no session id, whatever the developer's shell has."""
    for name in SESSION_ID_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """Two open folders: `here` is a checkout of REPO, `there` of OTHER_REPO."""
    from witan import repo as repo_module

    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()
    mapping = {here: REPO, there: OTHER_REPO}
    branches = {here: "here-branch", there: "there-branch"}
    monkeypatch.setattr(repo_module, "detect_at", lambda path: mapping.get(path))
    monkeypatch.setattr(
        repo_module, "current_branch", lambda start=None: branches.get(start)
    )
    for name in ("WITAN_TARGET", "CLAUDE_PROJECT_DIR", "WITAN_REPO"):
        monkeypatch.delenv(name, raising=False)
    return here, there


def _spec(fake, kind="task", folders=(), cfg=None, **kwargs):
    from witan.cli.run_helpers import build_launch_spec

    kwargs.setdefault("started_with_target", False)
    return build_launch_spec(
        kind,
        "tk-a",
        server=fake,
        cfg=cfg or _Cfg(),
        workspace_folders=list(folders),
        **kwargs,
    )


def test_a_task_spec_has_every_field_a_client_runs(folders):
    here, _ = folders
    spec = _spec(_Fake(task=_task(), claim=_CLAIMED), folders=[here])

    assert spec["version"] == 1
    assert spec["kind"] == "task"
    assert spec["launchable"] is True
    assert spec["argv"] == ["claude", "--model", "opus", spec["prompt"]]
    assert spec["cwd"] == str(here)
    assert (spec["agent"], spec["model"]) == ("claude", "opus")
    assert "claimed for you" in spec["prompt"]
    # The warning advises a session id, and a bare launch claim is the intent.
    assert "warning" not in spec["claim"]
    assert spec["claim"]["claimed_at"] == _CLAIMED["claimed_at"]
    # No session id travels down (ADR 0012, "Why the spec carries no session id").
    assert "WITAN_SESSION_ID" not in json.dumps(spec)


def test_the_local_store_unsets_the_target_and_a_named_one_pins_it(folders):
    here, _ = folders
    fake = _Fake(task=_task(), claim=_CLAIMED)

    local = _spec(fake, folders=[here])
    assert local["env"] == {}
    assert local["unset_env"] == [
        *SESSION_ID_ENV_VARS,
        "CLAUDE_PROJECT_DIR",
        "WITAN_REPO",
        "WITAN_TARGET",
    ]

    cfg = _Cfg()
    cfg.target_name = "prod"
    named = _spec(fake, folders=[here], cfg=cfg)
    assert named["env"] == {"WITAN_TARGET": "prod"}
    assert "WITAN_TARGET" not in named["unset_env"]


def test_handoff_only_when_the_editor_env_resolves_what_the_spec_does(
    folders, monkeypatch
):
    here, there = folders
    fake = _Fake(task=_task(), claim=_CLAIMED)

    assert _spec(fake, folders=[here])["handoff"] is True
    assert _spec(fake, folders=[here, there])["handoff"] is False
    assert _spec(fake, folders=[here], started_with_target=True)["handoff"] is False
    monkeypatch.setenv("WITAN_REPO", REPO)
    assert _spec(fake, folders=[here])["handoff"] is False


def test_the_session_env_is_stripped_before_the_claim(folders, monkeypatch):
    """Otherwise a launcher inside an agent session claims as that session, and
    the launched agent's renewal is refused as someone else's."""
    here, _ = folders
    for name in SESSION_ID_ENV_VARS:
        monkeypatch.setenv(name, f"{name.lower()}-outer")
    fake = _Fake(task=_task(), claim=_CLAIMED)

    _spec(fake, folders=[here])

    assert fake.claims == [dict.fromkeys(SESSION_ID_ENV_VARS)]


def test_the_session_env_is_back_after_the_claim(folders, monkeypatch):
    """The language server is long-lived; its later calls still need its id."""
    import os

    here, _ = folders
    monkeypatch.setenv("CLAUDE_SESSION_ID", "lsp-session")

    _spec(_Fake(task=_task(), claim=_CLAIMED), folders=[here])

    assert os.environ["CLAUDE_SESSION_ID"] == "lsp-session"


def test_no_workspace_is_refused_before_the_claim(folders):
    _, there = folders
    fake = _Fake(task=_task(), claim=_CLAIMED)

    spec = _spec(fake, folders=[there])

    assert spec["launchable"] is False
    assert spec["reason"] == "no_workspace"
    assert spec["repos"] == [REPO]
    assert "argv" not in spec
    # A refusal never leaves a lease behind.
    assert fake.claims == []


def test_another_open_folder_that_maps_to_the_repo_is_used(folders):
    here, there = folders

    fake = _Fake(task=_task(), claim=_CLAIMED)
    spec = _spec(fake, folders=[there, here])

    assert spec["cwd"] == str(here)
    # The claim links the folder the launch runs in, not the invoking one.
    assert fake.checkouts == [(REPO, "here-branch")]


def test_a_folder_with_no_branch_or_repo_links_no_branch(folders, monkeypatch):
    """Left to the server, an omitted branch falls back to the launcher's own."""
    from witan import repo as repo_module

    here, _ = folders
    fake = _Fake(task=_task(), claim=_CLAIMED)

    monkeypatch.setattr(repo_module, "current_branch", lambda start=None: None)
    _spec(fake, folders=[here])
    nowhere = here.parent / "nowhere"
    nowhere.mkdir()
    fake.task = _task(repo=None)
    _spec(fake, folders=[nowhere])

    assert fake.checkouts == [(REPO, ""), (None, "")]


def test_a_task_with_no_repo_runs_in_the_invoking_folder(folders):
    _, there = folders

    spec = _spec(_Fake(task=_task(repo=None), claim=_CLAIMED), folders=[there])

    assert spec["cwd"] == str(there)


@pytest.mark.parametrize("kind", ["task", "project"])
def test_no_open_folder_is_refused_for_a_record_with_no_repo(kind):
    """An editor with no folder open still gets a refusal, not an IndexError."""
    project = {"slug": "wp-a", "title": "P", "repos": []}
    fake = _Fake(task=_task(repo=None), project=project, claim=_CLAIMED)

    spec = _spec(fake, kind=kind, folders=[])

    assert (spec["launchable"], spec["reason"]) == (False, "no_workspace")
    assert fake.claims == []


def test_a_missing_task_is_not_found(folders):
    here, _ = folders
    fake = _Fake(task=None)

    spec = _spec(fake, folders=[here])

    assert (spec["launchable"], spec["reason"]) == (False, "not_found")
    assert fake.claims == []


def test_a_refused_claim_is_passed_through_with_no_argv(folders):
    here, _ = folders
    held = {
        "slug": "tk-a",
        "claimed": False,
        "reason": "held",
        "held_by": "someone-else",
        "remedy": "use --force",
    }

    spec = _spec(_Fake(task=_task(), claim=held), folders=[here])

    assert spec["launchable"] is False
    assert spec["reason"] == "held"
    assert spec["claim"] == held
    assert "argv" not in spec


def test_the_claim_never_prompts_at_a_terminal(folders, monkeypatch):
    """A steal offer would print its question to stdout ahead of the JSON and
    block on an answer nobody sees; `force` is how a launcher steals."""
    from witan_core import elicit

    here, _ = folders
    monkeypatch.setattr(elicit, "has_terminal", lambda: True)
    seen = {}

    class Async(_Fake):
        async def task_claim(self, slug, force=False, repo=None, branch=None, ctx=None):
            seen["ctx"] = ctx
            return _CLAIMED

    _spec(Async(task=_task()), folders=[here])

    assert seen == {"ctx": None}


def test_prompts_off_withholds_the_remote_elicitation_handler(monkeypatch):
    from witan.prompts import prompts_off
    from witan.remote.proxy import RemoteServerProxy
    from witan_core import elicit

    monkeypatch.setattr(elicit, "has_terminal", lambda: True)
    proxy = object.__new__(RemoteServerProxy)

    assert proxy._elicitation_handler() is not None
    with prompts_off():
        assert proxy._elicitation_handler() is None


def test_no_claim_builds_an_unclaimed_spec(folders):
    """The chat-panel handoff path: the prompt tells the agent to claim."""
    here, _ = folders
    fake = _Fake(task=_task(), claim=_CLAIMED)

    spec = _spec(fake, folders=[here], claim=False)

    assert spec["claim"] is None
    assert fake.claims == []
    assert 'task_claim(slug="tk-a")' in spec["prompt"]


def test_a_project_launch_claims_nothing(folders):
    """It hands over up to 20 ready tasks; claiming them all would hold leases
    on work nobody starts."""
    here, there = folders
    project = {
        "slug": "wp-a",
        "title": "P",
        "phase": "implementation",
        "status": "active",
        "repos": [OTHER_REPO, REPO],
    }
    fake = _Fake(project=project, claim=_CLAIMED)

    spec = _spec(fake, kind="project", folders=[here])

    assert spec["launchable"] is True
    assert spec["claim"] is None
    assert fake.claims == []
    assert spec["cwd"] == str(here)
    assert "tk-r" in spec["prompt"]

    fake.project = {**project, "repos": ["https://github.com/test/elsewhere"]}
    refused = _spec(fake, kind="project", folders=[here, there])
    assert refused["reason"] == "no_workspace"


# ── the CLI around it ─────────────────────────────────────────────────


def _cli(monkeypatch, fake, folder):
    from witan import config as cfg_module
    from witan.cli import _common

    monkeypatch.setattr(_common, "_server", fake)
    monkeypatch.setattr(cfg_module, "load", lambda **kw: _Cfg())
    monkeypatch.chdir(folder)


def test_emit_spec_prints_the_spec_and_does_not_launch(folders, monkeypatch, capsys):
    from witan.cli import run_helpers, tasks

    here, _ = folders
    _cli(monkeypatch, _Fake(task=_task(), claim=_CLAIMED), here)
    launched = []
    monkeypatch.setattr(
        run_helpers, "_launch_agent", lambda *a, **kw: launched.append(a)
    )

    tasks.task_run("tk-a", emit_spec=True)

    spec = json.loads(capsys.readouterr().out)
    assert spec["launchable"] is True
    assert spec["cwd"] == str(here)
    assert not launched


def test_emit_spec_exits_non_zero_on_a_refusal(folders, monkeypatch, capsys):
    from witan.cli import projects

    _, there = folders
    project = {"slug": "wp-a", "title": "P", "repos": [REPO]}
    _cli(monkeypatch, _Fake(project=project), there)

    with pytest.raises(SystemExit) as exited:
        projects.project_run("wp-a", emit_spec=True)

    assert exited.value.code == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "no_workspace"


def test_the_cli_launch_runs_the_spec_in_its_folder_and_env(folders, monkeypatch):
    from witan.cli import run_helpers, tasks

    here, _ = folders
    _cli(monkeypatch, _Fake(task=_task(), claim=_CLAIMED), here)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/somewhere/else")
    monkeypatch.setenv("WITAN_TARGET", "inherited")
    launched = []
    monkeypatch.setattr(
        run_helpers, "_launch_agent", lambda *a, **kw: launched.append(kw)
    )

    tasks.task_run("tk-a")

    (kw,) = launched
    assert Path(kw["cwd"]) == here
    assert "CLAUDE_PROJECT_DIR" not in kw["env"]
    assert "WITAN_TARGET" not in kw["env"]


def test_the_pickers_consolidated_launch_gets_the_launch_env(folders, monkeypatch):
    """Claimed bare, so its agent must not inherit the outer session either."""
    import os

    from witan.cli import run_helpers, tasks

    here, _ = folders

    class Picker(_Fake):
        def task_ready(self, **_kwargs):
            return [_task(), {**_task(), "slug": "tk-b"}]

    _cli(monkeypatch, Picker(claim=_CLAIMED), here)
    monkeypatch.setattr(tasks, "_pick_items", lambda items, _render: items)
    monkeypatch.setattr(tasks.Prompt, "ask", lambda *a, **kw: "1")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "outer")
    monkeypatch.setenv("WITAN_REPO", REPO)
    launched = []
    monkeypatch.setattr(tasks, "_launch_agent", lambda *a, **kw: launched.append(kw))
    monkeypatch.setattr(run_helpers, "_launch_agent", lambda *a, **kw: None)

    tasks.task_run()

    (kw,) = launched
    for name in ("CLAUDE_SESSION_ID", "WITAN_REPO", "WITAN_TARGET"):
        assert name not in kw["env"]
    assert os.environ["CLAUDE_SESSION_ID"] == "outer"


# ── task_release's if_claimed_at fence ────────────────────────────────


class _Releaser:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def task_release(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def test_cli_release_sends_the_fence_only_when_given(folders, monkeypatch, capsys):
    """A deployment older than `if_claimed_at` rejects it as an unknown argument."""
    from witan.cli import tasks

    here, _ = folders
    fake = _Releaser({"released": True, "status": "open"})
    _cli(monkeypatch, fake, here)

    tasks.task_release_cmd("tk-a")
    tasks.task_release_cmd("tk-a", if_claimed_at=_CLAIMED["claimed_at"])

    assert "if_claimed_at" not in fake.calls[0]
    assert fake.calls[1]["if_claimed_at"] == _CLAIMED["claimed_at"]


def test_cli_release_reports_a_changed_claim(folders, monkeypatch, capsys):
    from witan.cli import tasks

    here, _ = folders
    refused = {
        "released": False,
        "reason": "claim_changed",
        "remedy": "tk-a is no longer the claim taken at then.",
    }
    _cli(monkeypatch, _Releaser(refused), here)

    with pytest.raises(SystemExit) as exited:
        tasks.task_release_cmd("tk-a", if_claimed_at=_CLAIMED["claimed_at"])

    assert exited.value.code == 1
    out = capsys.readouterr().out
    assert "claim changed" in out
    assert "held by" not in out


@requires_omnigraph
def test_release_with_the_claims_own_instant_releases(server, outside_a_checkout):
    t = server.task_create(title="failed launch", description="x")
    claim = server.task_claim(t["slug"])

    released = server.task_release(t["slug"], if_claimed_at=claim["claimed_at"])

    assert released["released"] is True
    assert server.task_get(t["slug"])["status"] == "open"


@requires_omnigraph
def test_release_after_a_renewal_is_refused(server, outside_a_checkout):
    """The launched agent renewed, so the late release must leave its claim be."""
    t = server.task_create(title="launched", description="x")
    first = server.task_claim(t["slug"])
    renewed = server.task_claim(t["slug"])
    assert renewed["claimed_at"] != first["claimed_at"]

    refused = server.task_release(t["slug"], if_claimed_at=first["claimed_at"])

    assert refused["released"] is False
    assert refused["reason"] == "claim_changed"
    assert server.task_get(t["slug"])["status"] == "in_progress"


@requires_omnigraph
def test_release_of_a_closed_task_is_refused_even_with_force(
    server, outside_a_checkout
):
    t = server.task_create(title="done", description="x")
    claim = server.task_claim(t["slug"])
    server.task_close(t["slug"], resolution="done")

    refused = server.task_release(
        t["slug"], if_claimed_at=claim["claimed_at"], force=True
    )

    assert refused["reason"] == "claim_changed"
    assert server.task_get(t["slug"])["status"] == "closed"
