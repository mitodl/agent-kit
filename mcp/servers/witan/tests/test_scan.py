"""Unit tests for witan.scan — protocol, Finding model, and registry.

No omnigraph binary and no detection logic under test yet; these exercise the
extensible skeleton in isolation. Module-level fake scanners double as plugin
targets loaded by dotted path (e.g. ``tests.test_scan:PluginScanner``).
"""

import pytest

from witan.config import ScanConfig
from witan.scan import Finding, Scanner, ScannerError, ScannerRegistry, masked_preview
from witan.scan import registry as registry_mod


# ── Module-level fakes (also used as plugin-path / entry-point targets) ─────────


class FakeSecretScanner:
    name = "fake_secret"
    category = "secret"

    def __init__(self):
        self.calls = []

    def scan(self, text, field, node_type):
        self.calls.append((text, field, node_type))
        idx = text.find("SECRET")
        if idx == -1:
            return []
        return [
            Finding(
                detector=self.name,
                category="secret",
                start=idx,
                end=idx + 6,
                preview=masked_preview(self.name, "SECRET"),
            )
        ]


class PluginScanner:
    name = "plugin_email"
    category = "pii"

    def scan(self, text, field, node_type):
        return []


class BoomScanner:
    name = "boom"
    category = "secret"

    def scan(self, text, field, node_type):
        raise RuntimeError("kaboom")


class NotAScanner:
    """Missing name/category/scan — must be rejected by the registry."""


def _cfg(**kw) -> ScanConfig:
    return ScanConfig(**kw)


# ── Finding / masked_preview ────────────────────────────────────────────────────


def test_finding_span_and_frozen():
    f = Finding(detector="d", category="secret", start=3, end=9)
    assert f.span == (3, 9)
    assert f.severity == "high"
    assert f.action is None
    with pytest.raises(ValueError):
        f.detector = "other"


def test_masked_preview_never_leaks_the_value():
    preview = masked_preview("aws_secret_key", "hunter2SECRETvalue")
    assert "hunter2" not in preview
    assert "SECRET" not in preview
    assert "aws_secret_key" in preview
    assert "18 chars" in preview


def test_plain_object_satisfies_scanner_protocol():
    assert isinstance(FakeSecretScanner(), Scanner)
    assert not isinstance(NotAScanner(), Scanner)


# ── selection (allow/deny) ──────────────────────────────────────────────────────


def test_from_config_all_builtins_active_by_default():
    reg = ScannerRegistry.from_config(
        _cfg(), builtins=[FakeSecretScanner(), PluginScanner()]
    )
    assert {s.name for s in reg.scanners} == {"fake_secret", "plugin_email"}


def test_disabled_detectors_filtered_out():
    reg = ScannerRegistry.from_config(
        _cfg(disabled_detectors=["plugin_email"]),
        builtins=[FakeSecretScanner(), PluginScanner()],
    )
    assert [s.name for s in reg.scanners] == ["fake_secret"]


def test_enabled_detectors_restricts_to_named():
    reg = ScannerRegistry.from_config(
        _cfg(enabled_detectors=["plugin_email"]),
        builtins=[FakeSecretScanner(), PluginScanner()],
    )
    assert [s.name for s in reg.scanners] == ["plugin_email"]


def test_disabled_wins_over_enabled():
    reg = ScannerRegistry.from_config(
        _cfg(enabled_detectors=["fake_secret"], disabled_detectors=["fake_secret"]),
        builtins=[FakeSecretScanner()],
    )
    assert reg.scanners == []


# ── scan aggregation + error wrapping ───────────────────────────────────────────


def test_scan_aggregates_and_forwards_context():
    s1, s2 = FakeSecretScanner(), FakeSecretScanner()
    reg = ScannerRegistry([s1, s2])
    findings = reg.scan("a SECRET here", field="content", node_type="Memory")
    assert len(findings) == 2
    assert all(f.category == "secret" for f in findings)
    assert s1.calls == [("a SECRET here", "content", "Memory")]


def test_scan_returns_empty_when_nothing_matches():
    reg = ScannerRegistry([FakeSecretScanner()])
    assert reg.scan("nothing here", "content", "Memory") == []


def test_scan_wraps_scanner_failure():
    reg = ScannerRegistry([FakeSecretScanner(), BoomScanner()])
    with pytest.raises(ScannerError) as excinfo:
        reg.scan("x", "content", "Memory")
    assert excinfo.value.scanner == "boom"
    assert isinstance(excinfo.value.__cause__, RuntimeError)


# ── plugin-path loading ─────────────────────────────────────────────────────────


def test_plugin_path_loads_scanner():
    reg = ScannerRegistry.from_config(
        _cfg(plugins=["tests.test_scan:PluginScanner"]), builtins=[]
    )
    assert [s.name for s in reg.scanners] == ["plugin_email"]


def test_plugin_path_missing_colon_raises():
    with pytest.raises(ValueError, match="expected 'module.path:Attr'"):
        ScannerRegistry.from_config(_cfg(plugins=["tests.test_scan"]), builtins=[])


def test_plugin_path_missing_attr_raises():
    with pytest.raises(RuntimeError, match="has no attribute 'Nope'"):
        ScannerRegistry.from_config(_cfg(plugins=["tests.test_scan:Nope"]), builtins=[])


def test_plugin_path_non_scanner_raises():
    with pytest.raises(TypeError, match="not a valid Scanner"):
        ScannerRegistry.from_config(
            _cfg(plugins=["tests.test_scan:NotAScanner"]), builtins=[]
        )


def test_plugin_path_unimportable_module_raises():
    with pytest.raises(RuntimeError, match="Failed to import scanner plugin"):
        ScannerRegistry.from_config(
            _cfg(plugins=["witan._does_not_exist:Thing"]), builtins=[]
        )


# ── entry-point discovery ───────────────────────────────────────────────────────


class _FakeEP:
    def __init__(self, name, value, result):
        self.name = name
        self.value = value
        self._result = result

    def load(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _patch_entry_points(monkeypatch, eps):
    def fake_entry_points(group=None):
        return eps if group == registry_mod.ENTRY_POINT_GROUP else []

    monkeypatch.setattr(registry_mod.metadata, "entry_points", fake_entry_points)


def test_entry_point_scanner_discovered(monkeypatch):
    _patch_entry_points(
        monkeypatch, [_FakeEP("ep_email", "acme:PluginScanner", PluginScanner)]
    )
    reg = ScannerRegistry.from_config(_cfg(), builtins=[])
    assert [s.name for s in reg.scanners] == ["plugin_email"]


def test_entry_point_load_failure_raises(monkeypatch):
    _patch_entry_points(
        monkeypatch, [_FakeEP("ep_bad", "acme:Bad", ImportError("no module"))]
    )
    with pytest.raises(RuntimeError, match="Failed to load scanner plugin 'ep_bad'"):
        ScannerRegistry.from_config(_cfg(), builtins=[])
