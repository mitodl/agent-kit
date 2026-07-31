"""Per-repo store resolution and lazy initialisation."""

import os
import subprocess
from pathlib import Path

from . import config as cfg_module
from .graph import OmnigraphClient


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
        # Force apply on first creation — stamp will be written below.
        _schema_apply(binary, cfg.schema_file, store)
    else:
        # Only re-apply when the schema file has changed since the last apply.
        # schema apply is additive/idempotent but spawns a subprocess on every
        # PostToolUse reindex; the mtime sidecar avoids the cost on hot paths.
        _schema_apply_if_changed(binary, cfg.schema_file, store)

    # Record the canonical repo URI in a sidecar so listings can show it even for
    # a 0-file store (sanitize_slug is lossy — its `_` collapse isn't reversible).
    repo_sidecar(store).write_text(slug)
    return store


def repo_sidecar(store: Path) -> Path:
    """Sidecar file next to a store holding its canonical repo URI."""
    return store.parent / f"{store.name}.repo"


def _schema_stamp(store: Path) -> Path:
    return store.parent / f"{store.name}.schema_mtime"


def _schema_apply(binary: str, schema_file: Path, store: Path) -> None:
    res = subprocess.run(
        [binary, "schema", "apply", "--schema", str(schema_file), str(store)],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        _schema_stamp(store).write_text(str(schema_file.stat().st_mtime))


def _schema_apply_if_changed(binary: str, schema_file: Path, store: Path) -> None:
    stamp = _schema_stamp(store)
    current_mtime = str(schema_file.stat().st_mtime)
    if stamp.exists() and stamp.read_text().strip() == current_mtime:
        return
    _schema_apply(binary, schema_file, store)


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
    binary = _binary()
    if store.exists():
        # Pick up additive schema changes (new nodes/fields) on existing
        # stores; the mtime stamp keeps hot reindex paths subprocess-free.
        _schema_apply_if_changed(binary, cfg.bridge_schema_file, store)
        return store

    store.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [binary, "init", "--schema", str(cfg.bridge_schema_file), str(store)],
        check=True,
        capture_output=True,
        text=True,
    )
    _schema_apply(binary, cfg.bridge_schema_file, store)
    return store


def per_repo_stores(config: cfg_module.Config | None = None) -> list[Path]:
    """Every indexed per-repo store, sorted. Excludes the shared bridge store."""
    cfg = config or cfg_module.load()
    if not cfg.code_dir.is_dir():
        return []
    return [
        p
        for p in sorted(cfg.code_dir.glob("*.omni"))
        if p.name != cfg_module.BRIDGE_STORE_NAME
    ]


def repo_for_store(store: Path) -> str:
    """Canonical repo URI for a store: the exact sidecar if present, else a
    best-effort reconstruction from the (lossily) sanitized filename."""
    sidecar = repo_sidecar(store)
    if sidecar.exists():
        return sidecar.read_text().strip()
    return repo_from_stem(store.stem)


def repo_from_stem(stem: str) -> str:
    """Best-effort canonical repo URI from a sanitized store filename.

    The store name is ``sanitize_slug(repo)`` (``[/:]+`` collapsed to ``_``), so
    a 0-file store has no CodeFile to read the exact repo from. For the common
    ``scheme://host/path`` slug, reconstruct it: ``https_github.com_org_repo`` →
    ``https://github.com/org/repo``. A schemeless local slug is returned as-is.
    """
    for scheme in ("https", "http", "ssh"):
        prefix = f"{scheme}_"
        if stem.startswith(prefix):
            return f"{scheme}://{stem[len(prefix) :].replace('_', '/')}"
    return stem


def file_count(store: Path, config: cfg_module.Config | None = None) -> int | None:
    """How many files ``store`` has indexed, or None if it can't be read.

    Counts in the engine (``count_files``) rather than materializing a row per
    file: this runs per store in ``code_indexed_repos`` and on every prompt via
    the UserPromptSubmit hook, and the bulk read it replaced would also have
    undercounted a store past all_file_hashes' 1,000,000-row cap.
    """
    cfg = config or cfg_module.load()
    try:
        rows = OmnigraphClient(str(store), cfg.queries_dir).read(
            "code_read.gq", "count_files", {}
        )
    except Exception:  # noqa: BLE001 — degrade gracefully, a listing isn't critical
        return None
    if not rows:
        return 0
    # Read positionally: the column takes the match variable's name on a
    # populated store but is "?" on an empty one (see code_read.gq).
    return next(iter(rows[0].values()), 0)


def dir_stats(path: Path) -> tuple[int, float]:
    """Return (total_bytes, latest mtime as an epoch) in a single directory walk.

    The mtime stays an epoch rather than a formatted string so a caller reading
    a *remote* store's stats renders it in its own timezone, not the server's.

    ``os.walk`` rather than ``Path.rglob`` — a Lance store is thousands of
    fragment files, and this runs per store in ``code_indexed_repos`` plus on
    every prompt via the UserPromptSubmit hook, so the per-entry ``Path``
    construction is worth avoiding. Neither form follows directory symlinks.
    """
    total = 0
    latest = path.stat().st_mtime
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                # A broken symlink (which the previous is_file() check skipped),
                # or a file that vanished mid-walk — an index running alongside
                # this rewrites fragments constantly.
                continue
            total += st.st_size
            latest = max(latest, st.st_mtime)
    return total, latest


def _binary() -> str:
    return OmnigraphClient._find_binary()
