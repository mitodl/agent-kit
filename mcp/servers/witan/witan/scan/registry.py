"""Scanner discovery and aggregation (ADR 0001 §D2).

A :class:`ScannerRegistry` assembles the active set of scanners from three
sources — built-in detectors, ``witan.scanners`` entry-points, and
config-referenced dotted import paths — then applies the ``enabled_detectors`` /
``disabled_detectors`` allow/deny lists. It does not decide enforcement; it only
runs scanners and surfaces their findings (or a :class:`ScannerError`).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from importlib import metadata

from ..config import ScanConfig
from .models import Finding, Scanner, ScannerError

ENTRY_POINT_GROUP = "witan.scanners"


def builtin_scanners() -> list[Scanner]:
    """The scanners shipped with witan (built-in secret + PII detectors).

    Imported lazily so the regex set is only compiled when scanning is actually
    enabled (the registry is only built then).
    """
    from .detectors import default_scanners

    return default_scanners()


class ScannerRegistry:
    def __init__(
        self,
        scanners: Iterable[Scanner],
        sources: dict[str, str] | None = None,
    ) -> None:
        self._scanners: list[Scanner] = list(scanners)
        self._sources: dict[str, str] = dict(sources) if sources else {}

    @property
    def scanners(self) -> list[Scanner]:
        """The active scanners, in run order (after allow/deny selection)."""
        return list(self._scanners)

    def source_for(self, name: str) -> str:
        """Where an active scanner came from: ``built-in``, ``entry-point:<id>``,
        or ``config:<dotted.path>``. ``"unknown"`` for a scanner not built via
        :meth:`from_config` (e.g. one injected directly in a test)."""
        return self._sources.get(name, "unknown")

    @classmethod
    def from_config(
        cls,
        config: ScanConfig,
        *,
        builtins: Iterable[Scanner] | None = None,
    ) -> ScannerRegistry:
        """Build the active registry from config.

        ``builtins`` defaults to :func:`builtin_scanners`; tests inject their own.
        Plugin load failures raise loudly at build time — a policy configured to
        scan must not silently start with a detector missing.
        """
        discovered: list[tuple[Scanner, str]] = [
            (s, "built-in")
            for s in (builtin_scanners() if builtins is None else builtins)
        ]
        discovered.extend(_load_entry_point_scanners())
        discovered.extend(_load_plugin_paths(config.plugins))
        selected = _select(
            discovered, config.enabled_detectors, config.disabled_detectors
        )
        scanners = [s for s, _ in selected]
        sources = {_name(s): src for s, src in selected}
        return cls(scanners, sources)

    def scan(self, text: str, field: str, node_type: str) -> list[Finding]:
        """Run every active scanner and return the concatenated findings.

        A scanner that raises is wrapped in :class:`ScannerError` (naming it) and
        re-raised, so the enforcement layer can apply ``on_scanner_error``.
        """
        findings: list[Finding] = []
        for scanner in self._scanners:
            try:
                findings.extend(scanner.scan(text, field, node_type))
            except Exception as exc:  # noqa: BLE001 — policy boundary for third-party scanners
                raise ScannerError(_name(scanner), exc) from exc
        return findings


def _select(
    pairs: Iterable[tuple[Scanner, str]],
    enabled: list[str],
    disabled: list[str],
) -> list[tuple[Scanner, str]]:
    """Apply allow/deny. Empty ``enabled`` means "all"; ``disabled`` always wins."""
    allow = set(enabled)
    deny = set(disabled)
    result = []
    for scanner, source in pairs:
        name = _name(scanner)
        if name in deny:
            continue
        if allow and name not in allow:
            continue
        result.append((scanner, source))
    return result


def _load_entry_point_scanners() -> list[tuple[Scanner, str]]:
    out: list[tuple[Scanner, str]] = []
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            factory = ep.load()
        except Exception as exc:  # noqa: BLE001 — surface a clear plugin-load error
            raise RuntimeError(
                f"Failed to load scanner plugin {ep.name!r} ({ep.value}): {exc}"
            ) from exc
        scanner = _instantiate(factory, f"entry-point {ep.name!r}")
        out.append((scanner, f"entry-point:{ep.name}"))
    return out


def _load_plugin_paths(paths: Iterable[str]) -> list[tuple[Scanner, str]]:
    return [
        (_instantiate(_import_path(p), f"plugin {p!r}"), f"config:{p}") for p in paths
    ]


def _import_path(path: str) -> object:
    module_name, sep, attr = path.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(
            f"Invalid scanner plugin path {path!r}: expected 'module.path:Attr'."
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(f"Failed to import scanner plugin {path!r}: {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise RuntimeError(
            f"Scanner plugin {path!r}: module {module_name!r} has no attribute {attr!r}."
        ) from exc


def _instantiate(factory: object, label: str) -> Scanner:
    obj = factory() if callable(factory) else factory
    if not isinstance(obj, Scanner):
        raise TypeError(
            f"{label} is not a valid Scanner (needs name, category, and scan())."
        )
    return obj


def _name(scanner: Scanner) -> str:
    return getattr(scanner, "name", type(scanner).__name__)
