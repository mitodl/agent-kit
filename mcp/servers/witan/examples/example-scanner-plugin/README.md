# Example witan scanner plugin

A minimal, standalone package demonstrating how another organization plugs its
own detection rules into witan's write-path content scanning (see
[ADR 0001](../../docs/adr/0001-write-path-content-scanning.md) for the design
and [the operator/developer guide](../../docs/write-path-scanning.md) for the
full config surface and CLI), without forking witan.

## The contract

A scanner is anything with:

- `name: str` — a stable, unique detector id (used in `enabled_detectors` /
  `disabled_detectors` and in every `Finding.detector` it produces).
- `category: "secret" | "pii"` — the default enforcement bucket.
- `scan(text: str, field: str, node_type: str) -> list[Finding]` — inspect one
  free-text value and return zero or more findings. `field`/`node_type` let a
  rule be context-aware (e.g. only fire on `Task.description`).

`witan.scan.Scanner` is a `runtime_checkable` `Protocol` — structural typing,
not inheritance. A `Finding` just needs `detector`, `category`, `start`, `end`,
and optionally `severity` / `preview` / `action`; see `__init__.py` in this
package for a dependency-free implementation, or depend on `witan` and
construct `witan.scan.Finding` directly (adds validation, e.g. frozen fields).

**Never put the matched value itself in `preview`** — it's what ends up in
logs and (for blocked writes) the rejection error.

## Registering the plugin

Two ways, both read by `witan.scan.ScannerRegistry`:

1. **Entry point** (this package's approach) — declare it in `pyproject.toml`:

   ```toml
   [project.entry-points."witan.scanners"]
   acme_employee_id = "example_scanner_plugin:AcmeEmployeeIdScanner"
   ```

   Once the package is installed alongside `witan`, the scanner is discovered
   automatically — no config change needed.

2. **Dotted config path** — for a scanner that isn't packaged/published, point
   `ScanConfig.plugins` (or `WITAN_SCAN_PLUGINS`) at it directly:

   ```toml
   [scan]
   plugins = ["example_scanner_plugin:AcmeEmployeeIdScanner"]
   ```

Either way, `ScanConfig.enabled_detectors` / `disabled_detectors` can then
select or silence it like any built-in rule.

## Trying it locally

```bash
uv pip install -e examples/example-scanner-plugin
uv run python -c "
from witan.config import load_scan_config
from witan.scan import ScannerRegistry
reg = ScannerRegistry.from_config(load_scan_config())
print([s.name for s in reg.scanners])  # includes acme_employee_id
"
```

(Scanning is on by default — no `WITAN_SCAN_ENABLED` needed. Set it to
`false` to opt out.)
