import json

from agent_config_kit.jsonio import load_json_object, write_json


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
