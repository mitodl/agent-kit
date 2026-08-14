"""Write-path content scanning for witan (ADR 0001).

Public surface: the :class:`Finding` model, the :class:`Scanner` protocol that
built-in and third-party detectors implement, and the :class:`ScannerRegistry`
that discovers and runs them.
"""

from .audit import AuditEvent, AuditOutcome
from .enforce import (
    FIELD_MAP,
    WriteBlocked,
    WriteGuard,
    write_guard_from_config,
)
from .models import (
    Category,
    Finding,
    Scanner,
    ScannerError,
    Severity,
    masked_preview,
)
from .notice import (
    RedactionNotice,
    annotate,
    describe,
    no_redactions,
    take_redactions,
)
from .redact import REDACTED_TAG, flag_redacted, redact_spans
from .registry import ENTRY_POINT_GROUP, ScannerRegistry, builtin_scanners

__all__ = [
    "ENTRY_POINT_GROUP",
    "FIELD_MAP",
    "REDACTED_TAG",
    "AuditEvent",
    "AuditOutcome",
    "Category",
    "Finding",
    "RedactionNotice",
    "Scanner",
    "ScannerError",
    "ScannerRegistry",
    "Severity",
    "WriteBlocked",
    "WriteGuard",
    "annotate",
    "builtin_scanners",
    "describe",
    "flag_redacted",
    "masked_preview",
    "no_redactions",
    "redact_spans",
    "take_redactions",
    "write_guard_from_config",
]
