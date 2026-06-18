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
    binary = _binary()

    if not store.exists():
        store.parent.mkdir(parents=True, exist_ok=True)
        # ASSUMPTION: `omnigraph init --schema <file> <store>` is the per-store
        # init form, matching witan/install.sh.
        subprocess.run(
            [binary, "init", "--schema", str(cfg.schema_file), str(store)],
            check=True,
            capture_output=True,
            text=True,
        )

    # Apply the schema on EVERY run, not just on creation: it is idempotent
    # ("no changes" when matched) and additive, so an existing store picks up new
    # columns/indexes (e.g. Symbol.decorators) without a manual migration. Also
    # builds the FTS/BTREE indexes the search queries need. Best-effort.
    subprocess.run(
        [binary, "schema", "apply", "--schema", str(cfg.schema_file), str(store)],
        capture_output=True,
        text=True,
    )
    return store


def bridge_store(config: cfg_module.Config | None = None) -> Path:
    """Return the shared bridge store path without creating it."""
    cfg = config or cfg_module.load()
    return cfg_module.bridge_store_path(cfg.code_dir)


def ensure_bridge_store(config: cfg_module.Config | None = None) -> Path:
    """Resolve the shared bridge store, initialising it from bridge-schema.pg.

    Mirrors ``ensure_store`` but uses the bridge schema and the fixed
    ``_bridge.omni`` filename. ``schema apply`` builds the FTS index on
    ``key_norm`` that ``search_bindings`` needs.
    """
    cfg = config or cfg_module.load()
    store = cfg_module.bridge_store_path(cfg.code_dir)
    if store.exists():
        return store

    store.parent.mkdir(parents=True, exist_ok=True)
    binary = _binary()
    subprocess.run(
        [binary, "init", "--schema", str(cfg.bridge_schema_file), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            binary,
            "schema",
            "apply",
            "--schema",
            str(cfg.bridge_schema_file),
            str(store),
        ],
        capture_output=True,
        text=True,
    )
    return store


def _binary() -> str:
    binary = shutil.which("omnigraph")
    if binary is None:
        raise RuntimeError(
            "omnigraph binary not found on PATH. "
            "Run mcp/servers/witan-code/install.sh first."
        )
    return binary
