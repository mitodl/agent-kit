"""Package map: a repo's declared canonical package identity.

Read from ``witan-code.toml`` at the repo root (see docs/PACKAGE_MAP.md).
The identity qualifies provider symbol strings and feeds the
``known_provider_package`` confidence heuristic. Repos without the file get a
fallback identity derived from the repo URI so provider symbols always carry a
package qualifier.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_MAP_FILENAME = "witan-code.toml"

# SCIP empty-field convention (see docs/SYMBOL_FORMAT.md).
EMPTY = "."


@dataclass(frozen=True)
class PackageIdentity:
    name: str
    manager: str = EMPTY
    version: str = "main"
    provides: tuple[str, ...] = field(default_factory=tuple)
    """Extra published identities as ``"manager:name"`` strings."""

    declared: bool = False
    """True when read from witan-code.toml, False for a URI-derived fallback."""

    def provided_names(self) -> frozenset[str]:
        """Bare package names this repo provides (primary name + provides)."""
        names = {self.name}
        for entry in self.provides:
            _, _, name = entry.partition(":")
            names.add(name or entry)
        return frozenset(names)


def fallback_identity(repo: str) -> PackageIdentity:
    """Identity derived from the canonical repo URI when no TOML exists."""
    name = repo.rstrip("/").rsplit("/", 1)[-1] or repo
    return PackageIdentity(name=name)


def load(repo_root: Path, repo: str) -> PackageIdentity:
    """Load ``witan-code.toml`` from ``repo_root``, else the fallback identity.

    Malformed TOML or a missing ``[package].name`` degrades to the fallback —
    the package map must never make indexing fail.
    """
    path = repo_root / PACKAGE_MAP_FILENAME
    if not path.is_file():
        return fallback_identity(repo)
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return fallback_identity(repo)

    pkg = data.get("package")
    if not isinstance(pkg, dict) or not isinstance(pkg.get("name"), str):
        return fallback_identity(repo)

    provides = tuple(
        entry for entry in pkg.get("provides", ()) if isinstance(entry, str)
    )
    return PackageIdentity(
        name=pkg["name"],
        manager=str(pkg.get("manager", EMPTY)),
        version=str(pkg.get("version", "main")),
        provides=provides,
        declared=True,
    )
