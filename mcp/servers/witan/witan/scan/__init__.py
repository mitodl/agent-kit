"""Write-path content scanning for witan (ADR 0001).

Public surface: the :class:`Finding` model, the :class:`Scanner` protocol that
built-in and third-party detectors implement, and the :class:`ScannerRegistry`
that discovers and runs them.
"""

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
    "Category",
    "Finding",
    "Scanner",
    "ScannerError",
    "ScannerRegistry",
    "Severity",
    "builtin_scanners",
    "masked_preview",
]
