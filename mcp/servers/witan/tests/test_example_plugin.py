"""The example plugin in examples/example-scanner-plugin/ must actually load
and run through the real registry — it's the executable half of the plugin
contract's documentation. Loaded by dotted path (no install needed here);
entry-point discovery itself is covered separately in test_scan.py.
"""

import sys
from pathlib import Path

import pytest

from witan.config import ScanConfig
from witan.scan import Scanner, ScannerRegistry

EXAMPLE_SRC = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "example-scanner-plugin"
    / "src"
)


@pytest.fixture
def example_plugin_importable():
    sys.path.insert(0, str(EXAMPLE_SRC))
    try:
        yield
    finally:
        sys.path.remove(str(EXAMPLE_SRC))
        sys.modules.pop("example_scanner_plugin", None)


def test_example_plugin_satisfies_scanner_protocol(example_plugin_importable):
    from example_scanner_plugin import AcmeEmployeeIdScanner

    assert isinstance(AcmeEmployeeIdScanner(), Scanner)


def test_example_plugin_loads_via_dotted_path_config(example_plugin_importable):
    reg = ScannerRegistry.from_config(
        ScanConfig(plugins=["example_scanner_plugin:AcmeEmployeeIdScanner"]),
        builtins=[],
    )
    assert [s.name for s in reg.scanners] == ["acme_employee_id"]


def test_example_plugin_flags_employee_id(example_plugin_importable):
    reg = ScannerRegistry.from_config(
        ScanConfig(plugins=["example_scanner_plugin:AcmeEmployeeIdScanner"]),
        builtins=[],
    )
    findings = reg.scan("badge: ACME-EMP-482913 lost", "content", "Memory")
    assert len(findings) == 1
    f = findings[0]
    assert f.detector == "acme_employee_id"
    assert f.category == "pii"
    assert "482913" not in f.preview
