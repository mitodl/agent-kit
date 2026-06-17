import os
import re
from dataclasses import dataclass
from pathlib import Path

# Bundled query files, resolved relative to this file.
_QUERIES_DIR = Path(__file__).parent.parent / "queries"
_SCHEMA_FILE = Path(__file__).parent.parent / "schema" / "code-schema.pg"
_DEFAULT_CODE_DIR = Path.home() / ".local" / "share" / "omnigraph-memory" / "code"


@dataclass(frozen=True)
class Config:
    code_dir: Path
    """Directory holding per-repo code stores (one ``<slug>.omni`` each)."""

    author: str
    """Attribution string (carried for parity with Layer 1; unused on inserts)."""

    queries_dir: Path
    """Directory containing read.gq, mutations.gq, delete.gq."""

    schema_file: Path
    """Path to code-schema.pg, used to lazily init a per-repo store."""


def load() -> Config:
    """Load config from environment. All variables are optional with defaults."""
    return Config(
        code_dir=Path(
            os.environ.get("OMNIGRAPH_CODEGRAPH_DIR", str(_DEFAULT_CODE_DIR))
        ),
        author=os.environ.get(
            "OMNIGRAPH_CODEGRAPH_AUTHOR",
            os.environ.get("USER", "unknown"),
        ),
        queries_dir=_QUERIES_DIR,
        schema_file=_SCHEMA_FILE,
    )


def sanitize_slug(slug: str) -> str:
    """Make a repo slug safe for use as a filename component."""
    return re.sub(r"[/:]+", "_", slug).strip("_")


def store_path(slug: str, code_dir: Path | None = None) -> Path:
    """Resolve the per-repo store path for ``slug``."""
    base = code_dir or load().code_dir
    return base / f"{sanitize_slug(slug)}.omni"
