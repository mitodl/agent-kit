"""The CLI must not write to a local store while the caller believes otherwise.

Companion to ``test_remote_serve.py``: that one covers ``witan serve``'s half
of the silent-fallback defect (agent-kit#261), this one covers the CLI's.
"""

from __future__ import annotations

import pytest

DEPLOYED = """
[targets.production]
remote_url = "https://witan.example.org/mcp"
oidc_issuer = "https://sso.example.org/realms/eng"
match_paths = ["{matched}"]
"""

LOCAL_AND_DEPLOYED = (
    DEPLOYED
    + """
[targets.laptop]
server = "{store}"
match_paths = ["{local_path}"]
"""
)


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("WITAN_CONFIG", str(path))
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_REMOTE_URL", raising=False)
    monkeypatch.delenv("WITAN_MEMORY_URI", raising=False)
    return path


@pytest.fixture
def unmatched_cwd(monkeypatch, tmp_path):
    """Run from a directory no target claims — the reported reproduction.

    ``CLAUDE_PROJECT_DIR`` is what ``local_project_path`` prefers, so it has to
    be cleared too or the developer's own checkout decides the answer.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("WITAN_REPO", raising=False)
    return elsewhere


@pytest.fixture
def fresh_srv(monkeypatch):
    """Drop ``_srv``'s process-wide cache so each test resolves for itself."""
    from witan.cli import _common

    monkeypatch.setattr(_common, "_server", None)
    yield
    monkeypatch.setattr(_common, "_server", None)


def _stderr(monkeypatch):
    from rich.console import Console
    from witan.cli import _common

    recorder = Console(record=True, width=200, stderr=True)
    monkeypatch.setattr(_common, "stderr_console", recorder)
    monkeypatch.setattr("witan.cli.local_dispatch.stderr_console", recorder)
    return recorder


# ── the diagnosis itself ─────────────────────────────────────────────────────


def test_no_deployment_configured_is_not_ambiguous(config_file, unmatched_cwd):
    """An install with no deployed target keeps its previous behaviour exactly.

    There is no other graph the command could have meant, so nothing to warn
    about and nothing to refuse.
    """
    from witan import config as cfg

    config_file.write_text('[targets.laptop]\nserver = "/tmp/x.omni"\n')

    assert cfg.diagnose_local_dispatch() is None


def test_unmatched_directory_is_not_deliberate(config_file, unmatched_cwd, tmp_path):
    from witan import config as cfg

    config_file.write_text(DEPLOYED.format(matched=tmp_path / "code"))

    diagnosis = cfg.diagnose_local_dispatch()

    assert diagnosis is not None
    assert diagnosis.deliberate is False
    assert diagnosis.target_name is None
    assert diagnosis.deployed_targets == ("production",)
    assert diagnosis.graph_uri.endswith("graph.omni")


def test_a_matched_local_target_is_deliberate(config_file, monkeypatch, tmp_path):
    from witan import config as cfg

    here = tmp_path / "laptop-work"
    here.mkdir()
    monkeypatch.chdir(here)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    config_file.write_text(
        LOCAL_AND_DEPLOYED.format(
            matched=tmp_path / "code", store=tmp_path / "mine.omni", local_path=here
        )
    )

    diagnosis = cfg.diagnose_local_dispatch()

    assert diagnosis.deliberate is True
    assert diagnosis.target_name == "laptop"
    assert diagnosis.graph_uri == str(tmp_path / "mine.omni")


def test_explicit_memory_uri_is_deliberate(
    config_file, unmatched_cwd, monkeypatch, tmp_path
):
    """Naming the store outright is the escape hatch the refusal advertises."""
    from witan import config as cfg

    config_file.write_text(DEPLOYED.format(matched=tmp_path / "code"))
    monkeypatch.setenv("WITAN_MEMORY_URI", str(tmp_path / "chosen.omni"))

    diagnosis = cfg.diagnose_local_dispatch()

    assert diagnosis.deliberate is True
    assert diagnosis.graph_uri == str(tmp_path / "chosen.omni")


def test_global_server_is_deliberate(config_file, unmatched_cwd, tmp_path):
    """A global ``server =`` is the documented default-store setting.

    Someone who set one has said where unrouted work goes, so honour it rather
    than refusing every command they run outside a matched checkout.
    """
    from witan import config as cfg

    config_file.write_text(
        f'server = "{tmp_path / "default.omni"}"\n'
        + DEPLOYED.format(matched=tmp_path / "code")
    )

    diagnosis = cfg.diagnose_local_dispatch()

    assert diagnosis.deliberate is True
    assert diagnosis.graph_uri == str(tmp_path / "default.omni")


# ── what the CLI does with it ────────────────────────────────────────────────


def test_write_from_an_unmatched_directory_refuses(
    config_file, unmatched_cwd, fresh_srv, monkeypatch, tmp_path
):
    """The reported bug: ``witan task close`` reported success on the wrong graph."""
    from witan.cli._common import _srv

    config_file.write_text(DEPLOYED.format(matched=tmp_path / "code"))
    recorder = _stderr(monkeypatch)

    # getattr, not `_srv().task_close`: the refusal happens at attribute
    # lookup, deliberately — before the tool is even bound, let alone called.
    with pytest.raises(SystemExit) as exc:
        getattr(_srv(), "task_close")  # noqa: B009

    assert exc.value.code == 1
    text = recorder.export_text()
    assert "task_close" in text
    assert "graph.omni" in text
    assert "production" in text
    assert "WITAN_MEMORY_URI" in text


def test_read_from_an_unmatched_directory_is_allowed_but_announced(
    config_file, unmatched_cwd, fresh_srv, monkeypatch, tmp_path
):
    """Reads still work — a stale read is recoverable, a misrouted write is not."""
    from witan.cli._common import _srv

    config_file.write_text(DEPLOYED.format(matched=tmp_path / "code"))
    recorder = _stderr(monkeypatch)

    assert _srv().task_get is not None

    text = recorder.export_text()
    assert "graph.omni" in text
    assert "production" in text


def test_deliberate_local_target_dispatches_and_names_the_store(
    config_file, fresh_srv, monkeypatch, tmp_path
):
    from witan.cli._common import _srv

    here = tmp_path / "laptop-work"
    here.mkdir()
    monkeypatch.chdir(here)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    config_file.write_text(
        LOCAL_AND_DEPLOYED.format(
            matched=tmp_path / "code", store=tmp_path / "mine.omni", local_path=here
        )
    )
    recorder = _stderr(monkeypatch)

    from witan import server as server_module

    assert _srv() is server_module
    assert "mine.omni" in recorder.export_text()


def test_no_deployment_configured_dispatches_unwrapped(
    config_file, unmatched_cwd, fresh_srv
):
    from witan import server as server_module
    from witan.cli._common import _srv

    config_file.write_text('[targets.laptop]\nserver = "/tmp/x.omni"\n')

    assert _srv() is server_module


def test_every_read_tool_is_a_real_server_tool():
    """Guard the allowlist against drift.

    A name that no longer exists on the server would be silently permitted
    here and then fail at the call site with an AttributeError instead of the
    refusal this module is supposed to produce.
    """
    from witan import server as server_module
    from witan.cli.local_dispatch import READ_TOOLS

    missing = sorted(n for n in READ_TOOLS if not hasattr(server_module, n))

    assert missing == []


def test_writes_the_cli_dispatches_are_all_refused(
    config_file, unmatched_cwd, fresh_srv, monkeypatch, tmp_path
):
    """Every mutating tool the CLI reaches for, not just the reported one.

    The allowlist is what makes this hold for tools nobody has written yet:
    anything absent from READ_TOOLS refuses, so a new write tool is covered on
    the day it is added rather than the day someone remembers to list it.
    """
    from witan.cli._common import _srv

    config_file.write_text(DEPLOYED.format(matched=tmp_path / "code"))
    _stderr(monkeypatch)
    server = _srv()

    writes = [
        "task_create",
        "task_update",
        "task_close",
        "task_claim",
        "task_release",
        "task_link",
        "task_unlink",
        "memory_store",
        "memory_update",
        "memory_delete",
        "workflow_project_create",
        "workflow_project_update",
        "workflow_project_advance",
        "workflow_project_complete",
        "workflow_project_block",
        "workflow_project_unblock",
        "workflow_session_start",
        "workflow_session_end",
        "workflow_trace_mine",
        "store_merge",
        "merge_store",
        "apply_schema",
        "migrate_topics",
        "migrate_repo_keys",
    ]

    for name in writes:
        with pytest.raises(SystemExit):
            getattr(server, name)
