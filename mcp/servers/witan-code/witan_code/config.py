import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
from witan_core.config_file import load_toml as _load_toml_shared
from witan_core.target_config import (
    local_project_path,
    match_target,
    parse_target_tables,
    to_list,
)

from . import repo as repo_module

# Bundled query files, resolved relative to this file.
_QUERIES_DIR = Path(__file__).parent / "queries"
_SCHEMA_FILE = Path(__file__).parent / "schema" / "code-schema.pg"
_BRIDGE_SCHEMA_FILE = Path(__file__).parent / "schema" / "bridge-schema.pg"
_DEFAULT_CODE_DIR = Path.home() / ".local" / "share" / "witan" / "code"

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "witan" / "config.toml"
"""Shared with witan (witan-council) — see witan_core.config_file. A
[targets.<name>] block can carry ``code_dir`` here alongside witan's
``server``/``graph``/``token`` under the same name, routed together."""

# Filename of the single shared cross-repo bridge store, a sibling of the
# per-repo `<slug>.omni` stores in code_dir. Not routed through sanitize_slug
# (whose .strip("_") would eat the leading underscore); no real repo slug
# resolves to this name.
BRIDGE_STORE_NAME = "_bridge.omni"


@dataclass(frozen=True)
class Config:
    code_dir: Path
    """Directory holding per-repo code stores (one ``<slug>.omni`` each)."""

    author: str
    """Attribution string (carried for parity with Layer 1; unused on inserts)."""

    queries_dir: Path
    """Directory containing code_read.gq, code_mutations.gq, delete.gq."""

    schema_file: Path
    """Path to code-schema.pg, used to lazily init a per-repo store."""

    bridge_schema_file: Path
    """Path to bridge-schema.pg, used to lazily init the shared bridge store."""

    target_name: str | None = None
    """Name of the matched [targets.<name>] section, or None for global defaults."""


class _Target(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    code_dir: str | None = None
    author: str | None = None
    match_orgs: list[str] = Field(default_factory=list)
    match_repos: list[str] = Field(default_factory=list)
    match_hosts: list[str] = Field(default_factory=list)
    match_paths: list[str] = Field(default_factory=list)

    @field_validator(
        "match_orgs", "match_repos", "match_hosts", "match_paths", mode="before"
    )
    @classmethod
    def _normalize_match_list(cls, v: object) -> list[str]:
        return to_list(v)


def _load_toml() -> dict:
    """Load WITAN_CONFIG path or ~/.config/witan/config.toml. Returns {} on missing file.

    Reads the same file as witan (witan-council) — see DEFAULT_CONFIG_PATH.
    """
    return _load_toml_shared(DEFAULT_CONFIG_PATH)


def _parse_targets(raw: dict) -> list[_Target]:
    return [_Target(name=name, **cfg) for name, cfg in parse_target_tables(raw).items()]


def _first(*values: str | None, default: str | None = None) -> str | None:
    for v in values:
        if v:
            return v
    return default


def load(target: str | None = None) -> Config:
    """Load config from config.toml + environment, selecting a named target if applicable.

    Resolution order (highest -> lowest precedence):
    1. Environment variables (WITAN_CODE_DIR, WITAN_AUTHOR)
    2. Named target: ``target`` arg > WITAN_TARGET env var > auto-detect by
       repo or local checkout path
    3. Global values in config.toml
    4. Hardcoded defaults

    Shares config.toml (``WITAN_CONFIG`` / ``~/.config/witan/config.toml``)
    and the ``[targets.<name>]`` tables with witan (witan-council) — the same
    target block can carry witan's ``server``/``graph``/``token`` alongside
    this server's ``code_dir``, routed together. See ``witan.config.load()``'s
    docstring for the full ``match_paths``/``match_repos``/``match_hosts``/
    ``match_orgs`` precedence (shared logic: ``witan_core.target_config``).

    Raises ValueError for an explicitly-requested target that is not defined.
    """
    file_cfg = _load_toml()
    targets = _parse_targets(file_cfg)

    explicit = target or os.environ.get("WITAN_TARGET")
    if explicit:
        selected = next((t for t in targets if t.name == explicit), None)
        if selected is None:
            available = ", ".join(t.name for t in targets) or "(none defined)"
            raise ValueError(f"Unknown target {explicit!r}. Available: {available}")
    else:
        selected = match_target(
            targets, repo_uri=repo_module.detect(), local_path=local_project_path()
        )

    raw_code_dir = _first(
        os.environ.get("WITAN_CODE_DIR"),
        selected.code_dir if selected else None,
        file_cfg.get("code_dir"),
        default=str(_DEFAULT_CODE_DIR),
    )

    return Config(
        code_dir=Path(raw_code_dir).expanduser(),
        author=_first(
            os.environ.get("WITAN_AUTHOR"),
            selected.author if selected else None,
            file_cfg.get("author"),
            os.environ.get("USER"),
            default="unknown",
        ),
        queries_dir=_QUERIES_DIR,
        schema_file=_SCHEMA_FILE,
        bridge_schema_file=_BRIDGE_SCHEMA_FILE,
        target_name=selected.name if selected else None,
    )


def sanitize_slug(slug: str) -> str:
    """Make a repo slug safe for use as a LOCAL filename / branch-name component.

    Emits underscores (``[/:]+`` → ``_``). Fine for local ``<slug>.omni`` store
    dirs and per-repo branch prefixes, but NOT valid as a shared-cluster graph
    id — omnigraph graph ids must match ``^[a-zA-Z0-9-]{1,64}$`` (no
    underscores). Use :func:`graph_id` to derive the cluster ``--graph`` id.
    """
    return re.sub(r"[/:]+", "_", slug).strip("_")


# ── Shared-cluster graph id ──────────────────────────────────────────────────
#
# On the deployed omnigraph-server, each repo's code graph is a distinct cluster
# graph addressed as `--server <url> --graph <id>`. `graph_id()` is the CANONICAL
# repo-URI → graph-id function.
#
# SHARED CONTRACT — this exact algorithm is mirrored by ol-infrastructure's
# Pulumi provisioning (toolhive_witan/data_tier.py, which declares each
# `code-<repo>` graph in the cluster.yaml ConfigMap). witan-code selects the
# `--graph` id and provisioning declares the same id; they MUST agree
# byte-for-byte or a client will address a graph the cluster never created. Any
# change here has to land in lockstep on both sides — see task
# tk-code-graph-deployment-topology-shared-per-repo-c-cac400.
CODE_GRAPH_PREFIX = "code-"
# The shared cross-repo bridge graph (Layer 2.5), analogous to the local
# `_bridge.omni` store. Fixed id, not derived from any repo.
BRIDGE_GRAPH_ID = "code-bridge"
# omnigraph's graph-id constraint. Enforced by construction in `graph_id`.
_GRAPH_ID_MAX_LEN = 64
GRAPH_ID_RE = re.compile(r"^[a-zA-Z0-9-]{1,64}$")


def graph_id(repo: str) -> str:
    """Canonical cluster graph-id for ``repo``'s code graph.

    e.g. ``https://github.com/mitodl/ol-django`` → ``code-github-com-mitodl-ol-django``.

    Strip the URI scheme, collapse every run of non-alphanumerics to ``-``,
    lowercase, and prefix ``code-``. The result always satisfies
    :data:`GRAPH_ID_RE`. Slugs that would exceed :data:`_GRAPH_ID_MAX_LEN` are
    truncated and disambiguated with a hash of the full repo URI (so distinct
    long repos never collide on the same id).
    """
    body = re.sub(r"(?i)^[a-z][a-z0-9+.-]*://", "", repo)  # strip scheme
    body = re.sub(r"[^a-zA-Z0-9]+", "-", body).strip("-").lower()
    candidate = f"{CODE_GRAPH_PREFIX}{body}"
    if len(candidate) <= _GRAPH_ID_MAX_LEN:
        return candidate
    digest = hashlib.sha256(repo.encode()).hexdigest()[:8]
    keep = _GRAPH_ID_MAX_LEN - len(CODE_GRAPH_PREFIX) - len(digest) - 1
    return f"{CODE_GRAPH_PREFIX}{body[:keep].strip('-')}-{digest}"


def store_path(slug: str, code_dir: Path | None = None) -> Path:
    """Resolve the per-repo store path for ``slug``."""
    base = code_dir or load().code_dir
    return base / f"{sanitize_slug(slug)}.omni"


def bridge_store_path(code_dir: Path | None = None) -> Path:
    """Resolve the shared cross-repo bridge store path."""
    base = code_dir or load().code_dir
    return base / BRIDGE_STORE_NAME
