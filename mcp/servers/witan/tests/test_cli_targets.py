"""Tests for ``witan target add|list|remove``."""

from __future__ import annotations

import tomllib

import pytest


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    """Point WITAN_CONFIG at a path inside tmp_path and return it."""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("WITAN_CONFIG", str(path))
    monkeypatch.delenv("WITAN_TARGET", raising=False)
    monkeypatch.delenv("WITAN_REMOTE_URL", raising=False)
    monkeypatch.delenv("WITAN_OIDC_ISSUER", raising=False)
    return path


@pytest.fixture
def no_verify(monkeypatch):
    """Stub OIDC discovery — these tests are about config writing, not network."""
    calls = []

    def _fake(issuer, **kwargs):
        calls.append(issuer)
        return {"device_authorization_endpoint": "x", "token_endpoint": "y"}

    monkeypatch.setattr("witan_core.remote.oidc.discover_endpoints", _fake)
    return calls


def _capture(monkeypatch):
    from rich.console import Console

    from witan.cli import _common

    recorder = Console(record=True, width=200)
    monkeypatch.setattr(_common, "console", recorder)
    monkeypatch.setattr("witan.cli.targets.console", recorder)
    return recorder


def _targets_of(path):
    return tomllib.loads(path.read_text()).get("targets", {})


def test_add_writes_a_target_block(config_file, no_verify, monkeypatch):
    from witan.cli.targets import add

    _capture(monkeypatch)
    add(
        "hosted",
        remote_url="https://witan.example.org/mcp",
        oidc_issuer="https://sso.example.org/realms/eng",
        match_orgs=["my-org"],
    )

    targets = _targets_of(config_file)
    assert targets["hosted"]["remote_url"] == "https://witan.example.org/mcp"
    assert targets["hosted"]["oidc_issuer"] == "https://sso.example.org/realms/eng"
    assert targets["hosted"]["match_orgs"] == ["my-org"]
    assert no_verify == ["https://sso.example.org/realms/eng"]


def test_add_creates_config_when_absent_and_keeps_its_comments(
    config_file, no_verify, monkeypatch
):
    """Acceptance 1: a new user joins without hand-editing TOML."""
    from witan.cli.targets import add

    _capture(monkeypatch)
    assert not config_file.exists()
    add(
        "hosted",
        remote_url="https://witan.example.org/mcp",
        oidc_issuer="https://sso.example.org/realms/eng",
    )

    text = config_file.read_text()
    assert "[targets.hosted]" in text
    # The starter config is almost entirely explanatory comments; a TOML
    # round-trip would have silently dropped them.
    assert "# ── Named targets" in text
    assert _targets_of(config_file)["hosted"]["remote_url"]


def test_add_preserves_comments_and_existing_targets(
    config_file, no_verify, monkeypatch
):
    from witan.cli.targets import add

    config_file.write_text(
        "# a comment worth keeping\n"
        "author = 'Someone'\n"
        "\n"
        "[targets.personal]\n"
        "server = '/tmp/personal.omni'\n"
    )
    _capture(monkeypatch)
    add(
        "hosted",
        remote_url="https://witan.example.org/mcp",
        oidc_issuer="https://sso.example.org/realms/eng",
    )

    text = config_file.read_text()
    assert "# a comment worth keeping" in text
    targets = _targets_of(config_file)
    assert targets["personal"]["server"] == "/tmp/personal.omni"
    assert targets["hosted"]["remote_url"] == "https://witan.example.org/mcp"


def test_add_refuses_to_clobber_without_force(config_file, no_verify, monkeypatch):
    """Acceptance 2: never silently overwrite an existing target."""
    from witan.cli.targets import add

    config_file.write_text("[targets.hosted]\nremote_url = 'https://old.example/mcp'\n")
    recorder = _capture(monkeypatch)

    with pytest.raises(SystemExit):
        add(
            "hosted",
            remote_url="https://new.example/mcp",
            oidc_issuer="https://sso.example.org/realms/eng",
        )

    assert "already exists" in recorder.export_text()
    assert _targets_of(config_file)["hosted"]["remote_url"] == "https://old.example/mcp"


def test_add_force_replaces_in_place(config_file, no_verify, monkeypatch):
    from witan.cli.targets import add

    config_file.write_text(
        "# keep me\n[targets.hosted]\nremote_url = 'https://old.example/mcp'\n"
    )
    _capture(monkeypatch)
    add(
        "hosted",
        remote_url="https://new.example/mcp",
        oidc_issuer="https://sso.example.org/realms/eng",
        force=True,
    )

    targets = _targets_of(config_file)
    assert targets["hosted"]["remote_url"] == "https://new.example/mcp"
    # Exactly one block — a duplicate table would make tomllib raise above.
    assert config_file.read_text().count("[targets.hosted]") == 1
    assert "# keep me" in config_file.read_text()


def test_add_rejects_remote_url_without_issuer(config_file, monkeypatch):
    from witan.cli.targets import add

    recorder = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        add("hosted", remote_url="https://witan.example.org/mcp")

    assert "--oidc-issuer" in recorder.export_text()
    assert not config_file.exists()


def test_add_reports_a_bad_issuer_and_writes_nothing(config_file, monkeypatch):
    """The typo class this command exists to kill: caught at add time."""
    from witan_core.remote.oidc import RemoteAuthError

    from witan.cli.targets import add

    def _boom(issuer, **kwargs):
        raise RemoteAuthError("no such realm")

    monkeypatch.setattr("witan_core.remote.oidc.discover_endpoints", _boom)
    recorder = _capture(monkeypatch)

    with pytest.raises(SystemExit):
        add(
            "hosted",
            remote_url="https://witan.example.org/mcp",
            oidc_issuer="https://sso.example.org/realms/typo",
        )

    out = recorder.export_text()
    assert "Could not verify OIDC issuer" in out
    assert "--no-verify" in out
    assert not config_file.exists()


def test_add_no_verify_skips_the_network(config_file, monkeypatch):
    from witan.cli.targets import add

    def _boom(issuer, **kwargs):
        raise AssertionError("discovery must not be called with verify=False")

    monkeypatch.setattr("witan_core.remote.oidc.discover_endpoints", _boom)
    _capture(monkeypatch)
    add(
        "hosted",
        remote_url="https://witan.example.org/mcp",
        oidc_issuer="https://sso.example.org/realms/eng",
        verify=False,
    )

    assert _targets_of(config_file)["hosted"]["remote_url"]


def test_add_dry_run_writes_nothing(config_file, no_verify, monkeypatch):
    from witan.cli.targets import add

    recorder = _capture(monkeypatch)
    add(
        "hosted",
        remote_url="https://witan.example.org/mcp",
        oidc_issuer="https://sso.example.org/realms/eng",
        dry_run=True,
    )

    assert "[targets.hosted]" in recorder.export_text()
    assert not config_file.exists()
    assert no_verify == []


def test_add_rejects_a_name_that_is_not_a_bare_key(config_file, monkeypatch):
    from witan.cli.targets import add

    recorder = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        add("not a key", remote_url="https://x.example/mcp", oidc_issuer="https://i")

    assert "Invalid target name" in recorder.export_text()
    assert not config_file.exists()


def test_add_requires_something_to_register(config_file, monkeypatch):
    from witan.cli.targets import add

    recorder = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        add("empty")

    assert "Nothing to register" in recorder.export_text()


def test_written_target_is_readable_by_load_remote_config(
    config_file, no_verify, monkeypatch
):
    """Acceptance 3: the block the writer emits is the one the reader resolves."""
    from witan import config as cfg_module
    from witan.cli.targets import add

    _capture(monkeypatch)
    add(
        "hosted",
        remote_url="https://witan.example.org/mcp",
        oidc_issuer="https://sso.example.org/realms/eng",
        oidc_audience="witan",
    )

    remote = cfg_module.load_remote_config(target="hosted")
    assert remote is not None
    assert remote.url == "https://witan.example.org/mcp"
    assert remote.oidc_issuer == "https://sso.example.org/realms/eng"
    assert remote.oidc_audience == "witan"
    assert remote.target_name == "hosted"


def test_remove_deletes_only_that_block(config_file, monkeypatch):
    from witan.cli.targets import remove

    config_file.write_text(
        "author = 'Someone'\n"
        "\n"
        "[targets.hosted]\n"
        "remote_url = 'https://witan.example.org/mcp'\n"
        "\n"
        "[targets.personal]\n"
        "server = '/tmp/personal.omni'\n"
        "\n"
        "[rank]\n"
        "w_bm25 = 1.0\n"
    )
    _capture(monkeypatch)
    remove("hosted")

    parsed = tomllib.loads(config_file.read_text())
    assert "hosted" not in parsed["targets"]
    assert parsed["targets"]["personal"]["server"] == "/tmp/personal.omni"
    assert parsed["rank"]["w_bm25"] == 1.0
    assert parsed["author"] == "Someone"


def test_remove_unknown_target_exits(config_file, monkeypatch):
    from witan.cli.targets import remove

    config_file.write_text("[targets.personal]\nserver = '/tmp/p.omni'\n")
    recorder = _capture(monkeypatch)

    with pytest.raises(SystemExit):
        remove("hosted")

    out = recorder.export_text()
    assert "No target 'hosted'" in out
    assert "personal" in out


def test_remove_dry_run_writes_nothing(config_file, monkeypatch):
    from witan.cli.targets import remove

    original = "[targets.hosted]\nremote_url = 'https://witan.example.org/mcp'\n"
    config_file.write_text(original)
    _capture(monkeypatch)

    remove("hosted", dry_run=True)
    assert config_file.read_text() == original


def test_add_then_remove_round_trips(config_file, no_verify, monkeypatch):
    from witan.cli.targets import add, remove

    config_file.write_text("author = 'Someone'\n")
    _capture(monkeypatch)

    add(
        "hosted",
        remote_url="https://witan.example.org/mcp",
        oidc_issuer="https://sso.example.org/realms/eng",
    )
    remove("hosted")

    parsed = tomllib.loads(config_file.read_text())
    assert parsed.get("targets", {}) == {}
    assert parsed["author"] == "Someone"


def test_list_marks_the_matching_target(config_file, monkeypatch):
    from witan.cli.targets import list_targets

    config_file.write_text(
        "[targets.hosted]\n"
        "remote_url = 'https://witan.example.org/mcp'\n"
        "oidc_issuer = 'https://sso.example.org/realms/eng'\n"
        "match_orgs = ['mitodl']\n"
        "\n"
        "[targets.personal]\n"
        "server = '/tmp/personal.omni'\n"
    )
    monkeypatch.setenv("WITAN_REPO", "https://github.com/mitodl/agent-kit")
    recorder = _capture(monkeypatch)

    list_targets()

    out = recorder.export_text()
    assert "hosted" in out
    assert "personal" in out
    assert "*" in out


def test_list_with_no_targets_says_how_to_add_one(config_file, monkeypatch):
    from witan.cli.targets import list_targets

    config_file.write_text("author = 'Someone'\n")
    recorder = _capture(monkeypatch)

    list_targets()

    assert "No targets configured" in recorder.export_text()


def test_render_block_omits_empty_fields():
    from witan.cli.targets import render_target_block

    block = render_target_block(
        "hosted",
        {"remote_url": "https://x.example/mcp", "graph": None, "match_orgs": []},
    )
    assert block == '[targets.hosted]\nremote_url = "https://x.example/mcp"\n'


def test_render_block_writes_lists_inline():
    from witan.cli.targets import render_target_block

    block = render_target_block("hosted", {"match_orgs": ["a", "b"]})
    assert block == '[targets.hosted]\nmatch_orgs = ["a", "b"]\n'


def test_render_block_escapes_values():
    """Escaping is tomli_w's, not hand-rolled — a quote must survive a round trip."""
    from witan.cli.targets import render_target_block

    block = render_target_block("hosted", {"author": 'A "quoted" name'})
    assert tomllib.loads(block)["targets"]["hosted"]["author"] == 'A "quoted" name'


@pytest.mark.parametrize(
    "header",
    [
        "[targets.hosted]",
        '[targets."hosted"]',
        "[targets.'hosted']",
        "[ targets . hosted ]",
    ],
)
def test_remove_block_handles_every_toml_header_spelling(header):
    """All four declare the same table, so all four must be findable."""
    from witan.cli.targets import remove_target_block

    text = f'{header}\nremote_url = "x"\n\n[rank]\nw_bm25 = 1.0\n'
    new_text, removed = remove_target_block(text, "hosted")

    assert removed
    assert "[rank]" in new_text
    assert "hosted" not in new_text


def test_remove_keeps_the_comments_that_introduce_the_next_table():
    """Those comments document what follows, not the block being removed."""
    from witan.cli.targets import remove_target_block

    text = (
        "[targets.hosted]\n"
        "remote_url = 'https://old'\n"
        "\n"
        "# -- Personal store: side projects live here --\n"
        "# do not point this at work\n"
        "[targets.personal]\n"
        "server = '/tmp/p.omni'\n"
    )
    new_text, removed = remove_target_block(text, "hosted")

    assert removed
    assert "# -- Personal store" in new_text
    assert "# do not point this at work" in new_text
    assert tomllib.loads(new_text)["targets"] == {"personal": {"server": "/tmp/p.omni"}}


def test_force_replaces_a_literal_quoted_block_without_duplicating_it(
    config_file, no_verify, monkeypatch
):
    """A second [targets.hosted] table is a TOML error that bricks every command."""
    from witan.cli.targets import add

    config_file.write_text("[targets.'hosted']\nremote_url = 'https://old'\n")
    _capture(monkeypatch)

    add(
        "hosted",
        remote_url="https://new.example/mcp",
        oidc_issuer="https://sso.example.org/realms/eng",
        force=True,
    )

    # tomllib raises on a duplicate table, so parsing at all is the assertion.
    assert _targets_of(config_file)["hosted"]["remote_url"] == "https://new.example/mcp"


def test_force_replaces_in_place_preserving_target_order(
    config_file, no_verify, monkeypatch
):
    """match_target takes the first match — reordering silently reroutes repos."""
    from witan.cli.targets import add

    config_file.write_text(
        "[targets.hosted]\n"
        "remote_url = 'https://old'\n"
        "match_orgs = ['acme']\n"
        "\n"
        "[targets.fallback]\n"
        "server = '/tmp/f.omni'\n"
        "match_orgs = ['acme']\n"
    )
    _capture(monkeypatch)

    add(
        "hosted",
        remote_url="https://new.example/mcp",
        oidc_issuer="https://sso.example.org/realms/eng",
        match_orgs=["acme"],
        force=True,
    )

    assert list(_targets_of(config_file)) == ["hosted", "fallback"]


def test_config_is_written_as_utf8_regardless_of_locale(
    config_file, no_verify, monkeypatch
):
    """TOML is UTF-8 by spec, and the reader decodes it as such (load_toml is binary).

    The starter config is full of box-drawing characters, so a locale codec
    like cp1252 could not even encode it.
    """
    from witan.cli.targets import add

    _capture(monkeypatch)
    add(
        "hosted",
        remote_url="https://witan.example.org/mcp",
        oidc_issuer="https://sso.example.org/realms/eng",
        author="Ada Lovelace ☕ — née Byron",
    )

    raw = config_file.read_bytes()
    assert "née Byron".encode() in raw
    assert "── Named targets".encode() in raw
    decoded = raw.decode("utf-8")  # would raise if written in another codec
    assert tomllib.loads(decoded)["targets"]["hosted"]["author"].endswith("née Byron")


def test_write_leaves_no_temp_file_behind(config_file, no_verify, monkeypatch):
    from witan.cli.targets import add

    _capture(monkeypatch)
    add(
        "hosted",
        remote_url="https://witan.example.org/mcp",
        oidc_issuer="https://sso.example.org/realms/eng",
    )

    assert [p.name for p in config_file.parent.iterdir()] == [config_file.name]


def test_failed_write_leaves_the_old_config_intact(config_file, no_verify, monkeypatch):
    """The point of the temp-file dance: never a truncated config.toml."""
    from witan.cli import targets as targets_mod

    original = "author = 'Someone'\n[targets.personal]\nserver = '/tmp/p.omni'\n"
    config_file.write_text(original)
    _capture(monkeypatch)

    def _die(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(targets_mod.os, "replace", _die)

    with pytest.raises(OSError):
        targets_mod.add(
            "hosted",
            remote_url="https://witan.example.org/mcp",
            oidc_issuer="https://sso.example.org/realms/eng",
        )

    assert config_file.read_text() == original
    assert [p.name for p in config_file.parent.iterdir()] == [config_file.name]


def test_login_flag_requires_a_remote_url(config_file, monkeypatch):
    from witan.cli.targets import add

    recorder = _capture(monkeypatch)
    with pytest.raises(SystemExit):
        add("local", server="/tmp/local.omni", login=True)

    assert "--login needs --remote-url" in recorder.export_text()
    assert not config_file.exists()


def test_list_marks_the_pinned_target_not_the_matching_one(config_file, monkeypatch):
    """WITAN_TARGET wins for every other command; `*` must agree with it."""
    from witan.cli.targets import list_targets

    config_file.write_text(
        "[targets.hosted]\n"
        "server = '/tmp/h.omni'\n"
        "match_orgs = ['mitodl']\n"
        "\n"
        "[targets.personal]\n"
        "server = '/tmp/personal.omni'\n"
    )
    monkeypatch.setenv("WITAN_REPO", "https://github.com/mitodl/agent-kit")
    monkeypatch.setenv("WITAN_TARGET", "personal")
    recorder = _capture(monkeypatch)

    list_targets()

    marked = [
        line
        for line in recorder.export_text().splitlines()
        if "*" in line and ("hosted" in line or "personal" in line)
    ]
    assert len(marked) == 1
    assert "personal" in marked[0]
