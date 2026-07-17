import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

# Bundled query files, resolved relative to this file.
_QUERIES_DIR = Path(__file__).parent / "queries"
_SCHEMA_FILE = Path(__file__).parent / "schema" / "code-schema.pg"
_BRIDGE_SCHEMA_FILE = Path(__file__).parent / "schema" / "bridge-schema.pg"
_DEFAULT_CODE_DIR = Path.home() / ".local" / "share" / "witan" / "code"

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


def load() -> Config:
    """Load config from environment. All variables are optional with defaults."""
    return Config(
        code_dir=Path(os.environ.get("WITAN_CODE_DIR", str(_DEFAULT_CODE_DIR))),
        author=os.environ.get(
            "WITAN_AUTHOR",
            os.environ.get("USER", "unknown"),
        ),
        queries_dir=_QUERIES_DIR,
        schema_file=_SCHEMA_FILE,
        bridge_schema_file=_BRIDGE_SCHEMA_FILE,
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
