import json

from agent_config_kit.jsonio import json_diff, load_json_object, write_json


def test_load_json_object_missing_file_returns_empty_dict(tmp_path):
    assert load_json_object(tmp_path / "missing.json") == {}


def test_load_json_object_rejects_non_object_json(tmp_path):
    f = tmp_path / "config.json"
    f.write_text("[1, 2, 3]")
    assert load_json_object(f) is None


def test_load_json_object_tolerates_jsonc_comments_and_trailing_commas(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text('{\n  // a comment\n  "foo": "bar",\n}\n')
    assert load_json_object(f) == {"foo": "bar"}


def test_load_json_object_jsonc_stripping_preserves_urls_in_string_values(tmp_path):
    """A "//" inside a string value (e.g. a URL) must survive JSONC-comment
    stripping — only a "//" at the start of a line is a comment."""
    f = tmp_path / "settings.json"
    f.write_text(
        '{\n  // a comment\n  "url": "https://example.com/mcp",\n  "headers": {"Referer": "http://other.example/x"},\n}\n'
    )
    assert load_json_object(f) == {
        "url": "https://example.com/mcp",
        "headers": {"Referer": "http://other.example/x"},
    }


def test_load_json_object_unparsable_returns_none(tmp_path):
    f = tmp_path / "broken.json"
    f.write_text("{not json at all")
    assert load_json_object(f) is None


def test_write_json_dry_run_writes_nothing(tmp_path):
    path = tmp_path / "sub" / "out.json"
    write_json(path, {"a": 1}, dry_run=True)
    assert not path.exists()


def test_write_json_creates_parents_and_writes(tmp_path):
    path = tmp_path / "sub" / "out.json"
    write_json(path, {"a": 1}, dry_run=False)
    assert json.loads(path.read_text()) == {"a": 1}


def test_json_diff_empty_when_equal():
    assert json_diff({"a": 1}, {"a": 1}) == ""


def test_json_diff_shows_added_key():
    diff = json_diff({}, {"mcpServers": {"witan": {"command": "uvx"}}})
    assert diff.startswith("--- before")
    assert "+++ after" in diff
    assert '+  "mcpServers"' in diff
    assert '+      "command": "uvx"' in diff


def test_json_diff_shows_changed_value():
    diff = json_diff({"a": {"command": "old"}}, {"a": {"command": "new"}})
    assert '-    "command": "old"' in diff
    assert '+    "command": "new"' in diff


def test_json_diff_redacts_mcp_server_env_values():
    """`env` routinely carries API keys/tokens (StdioServer.env in
    models.py) — a diff must never print the actual value, in either
    direction, however deeply nested under a platform's own JSON shape."""
    before = {"mcpServers": {"witan": {"env": {}}}}
    after = {"mcpServers": {"witan": {"env": {"API_KEY": "sk-super-secret-123"}}}}
    diff = json_diff(before, after)
    assert "sk-super-secret-123" not in diff
    assert "API_KEY" in diff  # key name is fine to show, just not the value
    assert "<redacted>" in diff


def test_json_diff_redacts_remote_server_headers_and_oauth():
    before = {
        "grafana": {
            "headers": {"Authorization": "Bearer old-token"},
            "oauth": {"clientSecret": "old-secret"},
        }
    }
    after = {
        "grafana": {
            "headers": {"Authorization": "Bearer new-token"},
            "oauth": {"clientSecret": "new-secret"},
        }
    }
    diff = json_diff(before, after)
    assert "old-token" not in diff
    assert "new-token" not in diff
    assert "old-secret" not in diff
    assert "new-secret" not in diff


def test_json_diff_still_detects_a_change_entirely_inside_a_redacted_field():
    """A change that's only in a sensitive field's value must still be
    reported as *something* changed, even though the redacted lines
    themselves are identical before/after — never silently claim no diff."""
    before = {"witan": {"env": {"TOKEN": "old"}}}
    after = {"witan": {"env": {"TOKEN": "new"}}}
    assert json_diff(before, after) != ""


def test_json_diff_unaffected_when_only_non_sensitive_fields_change():
    diff = json_diff(
        {"witan": {"command": "old", "env": {"TOKEN": "x"}}},
        {"witan": {"command": "new", "env": {"TOKEN": "x"}}},
    )
    assert '-    "command": "old"' in diff
    assert '+    "command": "new"' in diff
