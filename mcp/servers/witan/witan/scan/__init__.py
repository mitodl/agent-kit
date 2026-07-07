"""Write-path content scanning for witan (ADR 0001).

Public surface: the :class:`Finding` model, the :class:`Scanner` protocol that
built-in and third-party detectors implement, and the :class:`ScannerRegistry`
that discovers and runs them.
"""

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
from .registry import ENTRY_POINT_GROUP, ScannerRegistry, builtin_scanners

__all__ = [
    "ENTRY_POINT_GROUP",
    "FIELD_MAP",
    "Category",
    "Finding",
    "Scanner",
    "ScannerError",
    "ScannerRegistry",
    "Severity",
    "WriteBlocked",
    "WriteGuard",
    "builtin_scanners",
    "masked_preview",
    "write_guard_from_config",
]
