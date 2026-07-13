"""Write-path scanning introspection: ``witan scan …`` (ADR 0001).

Lets an operator validate policy and debug false positives without writing to
the store: ``scan test`` dry-runs the active detectors against an ad-hoc
string, ``scan rules`` lists what's active and where it came from.
"""

from __future__ import annotations

import cyclopts

from .. import config as cfg_module
from ..scan import ScannerRegistry, redact_spans
from ..scan.allowlist import compile_allowlist, suppression_reason
from ._common import app, console, render_table

scan_app = cyclopts.App(
    name="scan", help="Introspect and dry-run write-path content scanning (ADR 0001)."
)
app.command(scan_app, name="scan")

_MODE_STYLE = {"block": "bold red", "redact": "yellow", "warn": "dim"}


def _mode_for(cfg: cfg_module.ScanConfig, category: str, action: str | None) -> str:
    return action or (cfg.pii_action if category == "pii" else cfg.secret_action)


@scan_app.command
def test(
    text: str,
    *,
    field: str = "content",
    node_type: str = "Memory",
) -> None:
    """Dry-run active detectors against TEXT and print findings. Nothing is written.

    Runs the exact same :class:`~witan.scan.ScannerRegistry` the write path
    uses, so a clean run here means the write path will accept ``text``
    unchanged. Findings are reported with their secret-free preview only —
    the matched text is never printed.

    Parameters
    ----------
    text: The string to scan.
    field: Field name to report context for (some detectors are field-aware,
        e.g. skipping ``author``).
    node_type: Node type to report context for (some detectors are node-aware).
    """
    cfg = cfg_module.load_scan_config()
    if not cfg.enabled:
        console.print(
            "[yellow]Note: scanning is disabled (WITAN_SCAN_ENABLED=false) — "
            "the write path would not scan this. Detectors still run below "
            "for validation.[/yellow]"
        )
    registry = ScannerRegistry.from_config(cfg)
    findings = registry.scan(text, field, node_type)
    if not findings:
        console.print("[green]No findings.[/green]")
        return

    allowlist = compile_allowlist(cfg.allowlist)
    reasons = {f: suppression_reason(f, text, cfg, allowlist) for f in findings}

    rows_data = []
    for f in findings:
        reason = reasons[f]
        mode = "warn" if reason else _mode_for(cfg, f.category, f.action)
        rows_data.append(
            {
                "detector": f.detector,
                "category": f.category,
                "severity": f.severity,
                "span": f"{f.start}-{f.end}",
                "action": mode,
                "suppressed": reason or "",
                "preview": f.preview,
            }
        )
    render_table(
        title="Findings",
        columns=[
            "detector",
            "category",
            "severity",
            "span",
            "action",
            "suppressed",
            "preview",
        ],
        rows=rows_data,
        styles={"action": _MODE_STYLE},
        dim_if_present={"suppressed"},
    )

    unsuppressed = [f for f in findings if reasons[f] is None]
    console.print(f"\n[dim]Redacted preview:[/dim] {redact_spans(text, unsuppressed)}")

    blocking = [
        f for f in unsuppressed if _mode_for(cfg, f.category, f.action) == "block"
    ]
    if blocking:
        console.print(
            f"\n[bold red]{len(blocking)} finding(s) would block this write.[/bold red]"
        )
    suppressed_count = len(findings) - len(unsuppressed)
    if suppressed_count:
        console.print(
            f"[dim]{suppressed_count} finding(s) allowlisted — downgraded to audit-only.[/dim]"
        )


@scan_app.command
def rules() -> None:
    """List active detectors: category, source, and enforcement mode."""
    cfg = cfg_module.load_scan_config()
    registry = ScannerRegistry.from_config(cfg)
    scanners = registry.scanners

    status = "[green]enabled[/green]" if cfg.enabled else "[dim]disabled[/dim]"
    console.print(f"Scanning: {status}  (on_scanner_error={cfg.on_scanner_error})\n")

    if not scanners:
        console.print("[dim]No active detectors.[/dim]")
        return

    rows_data = [
        {
            "detector": s.name,
            "category": s.category,
            "mode": _mode_for(cfg, s.category, None),
            "source": registry.source_for(s.name),
        }
        for s in sorted(scanners, key=lambda s: (s.category, s.name))
    ]
    render_table(
        title="Active detectors",
        columns=["detector", "category", "mode", "source"],
        rows=rows_data,
        styles={"mode": _MODE_STYLE},
    )
