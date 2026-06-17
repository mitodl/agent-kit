"""Per-repo store resolution and lazy initialisation."""

import shutil
import subprocess
from pathlib import Path

from . import config as cfg_module


def store_for_repo(slug: str, config: cfg_module.Config | None = None) -> Path:
    """Return the store path for ``slug`` without creating it."""
    cfg = config or cfg_module.load()
    return cfg_module.store_path(slug, cfg.code_dir)


def store_exists(slug: str, config: cfg_module.Config | None = None) -> bool:
    return store_for_repo(slug, config).exists()


def ensure_store(slug: str, config: cfg_module.Config | None = None) -> Path:
    """Resolve the per-repo store, initialising it from the schema if missing.

    Mirrors install.sh: ``omnigraph init --schema <schema> <store>``. The flag
    style is copied from the memory install script (the only place a per-store
    init is evidenced); see ASSUMPTION note below.
    """
    cfg = config or cfg_module.load()
    store = cfg_module.store_path(slug, cfg.code_dir)
    if store.exists():
        return store

    store.parent.mkdir(parents=True, exist_ok=True)
    binary = _binary()
    # ASSUMPTION: `omnigraph init --schema <file> <store>` is the per-store init
    # form, matching omnigraph-memory/install.sh. Not independently verifiable
    # here without the binary installed.
    subprocess.run(
        [binary, "init", "--schema", str(cfg.schema_file), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )
    # `schema apply` builds the FTS/BTREE indexes (the BM25 search_symbols query
    # needs an FTS index on qualified_name). Mirrors omnigraph-memory/install.sh;
    # best-effort so a CLI that folds this into `init` does not break init.
    subprocess.run(
        [binary, "schema", "apply", "--schema", str(cfg.schema_file), str(store)],
        capture_output=True,
        text=True,
    )
    return store


def _binary() -> str:
    binary = shutil.which("omnigraph")
    if binary is None:
        raise RuntimeError(
            "omnigraph binary not found on PATH. "
            "Run mcp/servers/omnigraph-codegraph/install.sh first."
        )
    return binary
