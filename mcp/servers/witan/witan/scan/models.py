"""Core types for write-path content scanning (ADR 0001 §D2).

A :class:`Scanner` inspects a single free-text value and returns zero or more
:class:`Finding` objects. A Finding describes *what* was matched and *where* —
never the matched value itself. The enforcement layer maps a Finding's
``category`` to a configured action (block/redact/warn); a Finding may also
carry an explicit ``action`` to override that category default.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..config import ScanAction

Category = Literal["secret", "pii"]
Severity = Literal["low", "medium", "high", "critical"]


def masked_preview(detector: str, value: str) -> str:
    """A secret-free descriptor of a matched value, safe for logs and errors.

    Deliberately includes **no character** of ``value`` — surfacing even a
    fragment of a secret in an exception or audit line would defeat the control
    (ADR 0001 §D3). Callers who want a stable correlation id across occurrences
    should hash the value separately with a salt.
    """
    return f"<{detector}: {len(value)} chars>"


class Finding(BaseModel):
    """A single detection within a scanned value."""

    model_config = ConfigDict(frozen=True)

    detector: str
    """Stable id of the detector that produced this finding (e.g. ``aws_secret_key``)."""

    category: Category
    start: int
    end: int
    severity: Severity = "high"

    preview: str = ""
    """Secret-free descriptor (see :func:`masked_preview`). Never the raw match."""

    action: ScanAction | None = None
    """Optional per-finding enforcement override. ``None`` means "use the
    configured action for ``category``"."""

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)


@runtime_checkable
class Scanner(Protocol):
    """The contract every detector — built-in or third-party plugin — implements.

    Implementations expose a unique ``name`` and a default ``category`` (for
    introspection and allow/deny selection) and a ``scan`` method. ``field`` and
    ``node_type`` let a scanner be context-aware (e.g. ignore the ``author``
    field for email detection).
    """

    name: str
    category: Category

    def scan(self, text: str, field: str, node_type: str) -> list[Finding]: ...


class ScannerError(RuntimeError):
    """Raised when a scanner itself fails during :meth:`ScannerRegistry.scan`.

    Names the offending scanner so the enforcement layer can apply its
    ``on_scanner_error`` policy (fail-closed by default) without conflating a
    broken detector with a clean scan.
    """

    def __init__(self, scanner: str, cause: BaseException) -> None:
        super().__init__(f"scanner {scanner!r} failed: {cause}")
        self.scanner = scanner
