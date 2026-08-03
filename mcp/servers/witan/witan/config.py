import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from witan_core.config_file import load_toml as _load_toml_shared
from witan_core.remote.config import RemoteConfig, resolve_remote_config
from witan_core.target_config import (
    local_project_path,
    match_target,
    parse_target_tables,
    to_list,
)

from . import repo as repo_module

_QUERIES_DIR = Path(__file__).parent.parent / "queries"
_DEFAULT_GRAPH_URI = Path.home() / ".local" / "share" / "witan" / "graph.omni"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "witan" / "config.toml"


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    graph_uri: str
    """Local path, s3://, or http(s):// URI pointing at the graph."""

    graph_name: str
    """omnigraph graph id addressed on a remote server (``--graph``). Selects one
    of the N graphs a single omnigraph-server serves; ignored for local/S3
    ``--store`` graphs. Env WITAN_MEMORY_GRAPH, default ``council``."""

    graph_token: str | None
    """Bearer token. Required when graph_uri is http(s)://. Unused for local/S3."""

    author: str
    """Attribution string written to Memory.author on every insert."""

    queries_dir: Path
    """Directory containing read.gq and mutations.gq."""

    agent: str
    """Default coding agent CLI to invoke (claude, pi, copilot, opencode, kilo)."""

    model: str | None
    """Default model passed through to the agent's --model flag."""

    target_name: str | None
    """Name of the matched [targets.<name>] section, or None for global defaults."""


class RankConfig(BaseModel):
    """Weights and decay for the composite memory re-rank (spec §7).

    Sourced from ``WITAN_RANK_*`` env vars (and the ``[rank]`` table in
    config.toml), defaulting to the constants below. Ranking is always on;
    these are tuning knobs, not a feature flag. Set every ``w_*`` to 0 to
    reproduce the raw BM25 order.
    """

    model_config = ConfigDict(frozen=True)

    w_bm25: float = 1.0
    w_recency: float = 0.3
    w_corrob: float = 0.2
    w_conf: float = 0.2
    half_life_days: float = Field(default=90.0, gt=0)
    default_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    penalty_superseded: float = 1.0
    penalty_contradicted: float = 0.25
    w_hop: float = 0.5
    """Per-hop distance penalty in graph-aware recall — seeds (hop 0) outrank
    expanded neighbours (hop ≥ 1)."""


_RANK_FIELDS = {
    "w_bm25": "WITAN_RANK_W_BM25",
    "w_recency": "WITAN_RANK_W_RECENCY",
    "w_corrob": "WITAN_RANK_W_CORROB",
    "w_conf": "WITAN_RANK_W_CONF",
    "half_life_days": "WITAN_RANK_HALFLIFE_DAYS",
    "default_confidence": "WITAN_RANK_DEFAULT_CONF",
    "penalty_superseded": "WITAN_RANK_PEN_SUPERSEDED",
    "penalty_contradicted": "WITAN_RANK_PEN_CONTRADICTED",
    "w_hop": "WITAN_RANK_W_HOP",
}


def _rank_config_error(exc: ValidationError, sources: dict[str, str]) -> ValueError:
    """Translate a RankConfig ValidationError into a source-attributed message."""
    err = exc.errors()[0]
    field = str(err["loc"][0])
    source = sources.get(field, field)
    if err["type"] in ("float_parsing", "float_type"):
        return ValueError(
            f"Invalid rank knob {source}={err['input']!r}: expected a number."
        )
    if field == "half_life_days" and err["type"] == "greater_than":
        return ValueError(f"Invalid rank knob {source}: half_life_days must be > 0.")
    if field == "default_confidence" and err["type"] in (
        "greater_than_equal",
        "less_than_equal",
    ):
        return ValueError(
            f"Invalid rank knob {source}: default_confidence must be between 0.0 and 1.0."
        )
    return ValueError(f"Invalid rank knob {source}: {err['msg']}")


def load_rank_config() -> RankConfig:
    """Resolve RankConfig from env > config.toml [rank] > constant defaults.

    Rejects values that would break ranking (non-positive half-life, confidence
    outside 0–1) so a misconfiguration fails loudly at startup rather than
    silently skewing or overflowing the score.
    """
    file_rank = _load_toml().get("rank", {})
    if not isinstance(file_rank, dict):
        raise ValueError("The 'rank' section in config must be a table.")

    raw: dict[str, object] = {}
    sources: dict[str, str] = {}
    for field, env_var in _RANK_FIELDS.items():
        value = os.environ.get(env_var)
        if value is not None:
            raw[field] = value
            sources[field] = env_var
        elif field in file_rank:
            raw[field] = file_rank[field]
            sources[field] = f"[rank].{field} in config.toml"

    try:
        return RankConfig(**raw)
    except ValidationError as exc:
        raise _rank_config_error(exc, sources) from exc


class IdentityConfig(BaseModel):
    """Keycloak JWT → omnigraph per-user actor mapping (ADR 0004).

    Sourced entirely from ``WITAN_OIDC_*``/``WITAN_ACTOR_TOKENS_FILE`` env
    vars — this is deployment/ops config for the shared ``streamable-http``
    service, not something an individual local user sets in config.toml.
    Unaffected by the stateless 2026-07-28 era (ADR-0006): the mapping reads
    the JWT on every request and never depended on session state.
    ``oidc_issuer`` unset means the deployed-auth path is disabled entirely
    (local ``stdio`` usage never sets it).
    """

    model_config = ConfigDict(frozen=True)

    oidc_issuer: str | None = None
    """Keycloak realm issuer URL, e.g. https://sso.example.org/realms/ol-platform-engineering."""

    oidc_audience: str | None = None
    """Expected JWT audience claim for this witan deployment. Required when
    oidc_issuer is set — an unchecked audience would accept a token minted
    for a different client."""

    actor_tokens_file: str | None = None
    """Path to the {actor_id: token} JSON map — same artifact omnigraph-server
    reads via OMNIGRAPH_SERVER_BEARER_TOKENS_FILE. Required when oidc_issuer
    is set."""


def load_identity_config() -> IdentityConfig:
    """Resolve IdentityConfig from WITAN_OIDC_ISSUER / _AUDIENCE / WITAN_ACTOR_TOKENS_FILE.

    Raises ValueError unless all three are set together, or none are — a
    half-configured deployment should fail loudly at startup rather than
    silently leave every request unauthenticated or unresolvable.
    ``oidc_audience`` is not optional here even though JWTVerifier itself
    treats a missing audience as "don't check": skipping audience validation
    would accept a token minted for a different client/application (a token
    substitution / confused-deputy risk), so witan requires it explicitly
    whenever OIDC is enabled at all.
    """
    issuer = os.environ.get("WITAN_OIDC_ISSUER")
    audience = os.environ.get("WITAN_OIDC_AUDIENCE")
    tokens_file = os.environ.get("WITAN_ACTOR_TOKENS_FILE")
    if not (bool(issuer) == bool(audience) == bool(tokens_file)):
        raise ValueError(
            "WITAN_OIDC_ISSUER, WITAN_OIDC_AUDIENCE, and WITAN_ACTOR_TOKENS_FILE "
            "must be set together: "
            f"WITAN_OIDC_ISSUER={issuer!r}, WITAN_OIDC_AUDIENCE={audience!r}, "
            f"WITAN_ACTOR_TOKENS_FILE={tokens_file!r}."
        )
    return IdentityConfig(
        oidc_issuer=issuer,
        oidc_audience=audience,
        actor_tokens_file=tokens_file,
    )


def load_remote_config(target: str | None = None) -> RemoteConfig | None:
    """Resolve RemoteConfig from env > named target > global config.toml > default.

    Target selection mirrors ``load()``: ``target`` arg > ``WITAN_TARGET`` env
    var > auto-detect by repo/local checkout path (``match_paths`` >
    ``match_repos`` > ``match_hosts`` > ``match_orgs`` —
    ``witan_core.target_config.match_target``). A ``[targets.<name>]`` block
    can carry ``remote_url``/``oidc_issuer``/``oidc_client_id``/
    ``oidc_audience`` alongside its omnigraph ``server``/``graph``/``token``,
    so one target routes both the store and which deployed witan service the
    CLI talks to. witan-code's CLI reads the same keys off the same target
    (``witan_code.config.load_remote_config``) — the deployment mounts both
    tool surfaces on one endpoint.

    Returns ``None`` when no ``url`` is configured from any source
    (in-process mode). Raises ``ValueError`` if a URL is configured without an
    issuer — a remote endpoint the CLI can't authenticate to is useless, so
    fail loudly rather than fall through to the unauthenticated in-process
    path — or if an explicitly-requested target is not defined.
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

    return resolve_remote_config(file_cfg, selected)


ScanAction = Literal["block", "redact", "warn"]
"""What to do when a scanner flags content on the write path (ADR 0001 §D3).

- ``block``  — reject the write; the mutation never reaches the store.
- ``redact`` — replace the matched span with a placeholder, then proceed.
- ``warn``   — emit an audit event and store the content unchanged.
"""


class ScanConfig(BaseModel):
    """Write-path content-scanning policy (ADR 0001, amended).

    Sourced from ``WITAN_SCAN_*`` env vars and the ``[scan]`` table in
    config.toml, defaulting to the constants below. Ships **enabled** —
    opt-out (set ``WITAN_SCAN_ENABLED=false`` or ``[scan] enabled = false`` to
    turn it off), unlike the ``WITAN_EMBED_ENABLED`` opt-in precedent this
    package originally followed.

    ``enabled_detectors``/``disabled_detectors``/``plugins``/``allowlist``/
    ``allowlist_hashes`` accept a TOML list or a comma-separated string
    (env-var ergonomics). An empty ``enabled_detectors`` means "every
    registered detector is active"; naming any detector switches to an
    explicit allowlist. ``disabled_detectors`` always wins over
    ``enabled_detectors``.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    """Master switch. When False the write path is not scanned at all."""

    secret_action: ScanAction = "block"
    """Enforcement for ``secret`` findings. Fail-closed by default."""

    pii_action: ScanAction = "redact"
    """Enforcement for ``pii`` findings. Mask-and-proceed by default."""

    enabled_detectors: list[str] = Field(default_factory=list)
    disabled_detectors: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    """Dotted import paths of external scanners to load in addition to those
    discovered via the ``witan.scanners`` entry-point group."""

    allowlist: list[str] = Field(default_factory=list)
    """Regexes whose matches are downgraded to audit-only (false-positive
    suppression). Tested against each finding's own matched span with
    ``re.fullmatch`` — see ``witan/scan/allowlist.py``."""

    allowlist_hashes: list[str] = Field(default_factory=list)
    """Salted SHA-256 digests (hex) of specific approved values, downgraded to
    audit-only like ``allowlist`` but without ever putting the plaintext value
    in config. Requires ``allowlist_salt``; a digest is computed as
    ``sha256(allowlist_salt + matched_span).hexdigest()``. Normalized to
    lowercase at load time — ``hexdigest()`` is always lowercase, so an
    operator-typed uppercase digest would otherwise silently never match."""

    allowlist_salt: str = ""
    """Salt for ``allowlist_hashes``. Empty means the hash allowlist is
    inert — set a deployment-specific value before relying on it."""

    on_scanner_error: Literal["block", "warn"] = "block"
    """What to do when a scanner itself raises. Fail-closed by default so a
    broken detector cannot silently open the gate."""

    overlay: dict[str, dict[str, object]] = Field(default_factory=dict)
    """Per-repo enforcement overrides: ``{repo_uri: {field: value, ...}}``.
    Sourced *only* from ``[scan.overlay."<repo-uri>"]`` TOML tables —
    deliberately no ``WITAN_SCAN_*`` env-var form (ADR 0001 amendment,
    2026-07-09): scan policy must stay admin-owned, and env vars are exactly
    the surface a write's own process could control. Keys are canonicalized
    with :func:`_canonical_repo` (scheme, ``.git``/trailing-slash, casing) so
    a TOML key and the write's own ``repo`` param that refer to the same repo
    in different spellings still match — see :meth:`for_repo`. Validated
    eagerly by :func:`load_scan_config`; applied per write by
    :meth:`for_repo`, which only ever overrides enforcement fields (actions,
    detector allow/deny, allowlists, ``on_scanner_error``) — never
    ``overlay`` itself."""

    def for_repo(self, repo: str | None) -> "ScanConfig":
        """The effective policy for a write tagged with ``repo`` — the base
        config with any matching ``overlay`` table applied on top. No match
        (including ``repo=None``) returns ``self`` unchanged, so this is a
        no-op for every deployment that doesn't configure an overlay."""
        if not repo or not self.overlay:
            return self
        overrides = self.overlay.get(_canonical_repo(repo))
        if not overrides:
            return self
        return self.model_copy(update=overrides)

    @field_validator(
        "enabled_detectors",
        "disabled_detectors",
        "plugins",
        "allowlist",
        "allowlist_hashes",
        mode="before",
    )
    @classmethod
    def _split_list(cls, v: object) -> list[str]:
        """Accept a TOML list or a comma-separated string (for env vars).

        Items are stripped and blanks dropped in both cases, so a stray space in
        a plugin path (from either source) can't become an unimportable entry.
        """
        items = v.split(",") if isinstance(v, str) else to_list(v)
        return [s.strip() for s in items if s.strip()]

    @field_validator("allowlist")
    @classmethod
    def _validate_allowlist_regexes(cls, v: list[str]) -> list[str]:
        """Fail loudly at load time for a bad pattern — matches the ADR's
        "misconfigured policy fails loudly" invariant, and means
        :func:`witan.scan.allowlist.compile_allowlist` never has to."""
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid allowlist regex {pattern!r}: {exc}") from exc
        return v

    @field_validator("allowlist_hashes")
    @classmethod
    def _normalize_hashes(cls, v: list[str]) -> list[str]:
        """``hashlib.sha256(...).hexdigest()`` is always lowercase; normalize
        so an operator-typed uppercase digest still matches."""
        return [h.lower() for h in v]

    @field_validator("overlay")
    @classmethod
    def _normalize_overlay_keys(
        cls, v: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        normalized: dict[str, dict[str, object]] = {}
        for key, overrides in v.items():
            if not key:
                raise ValueError("[scan.overlay] repo key must not be empty.")
            normalized[_canonical_repo(key)] = overrides
        return normalized


def _canonical_repo(value: str) -> str:
    """Canonicalize a repo string for ``[scan.overlay]`` matching.

    A superset of :func:`witan.repo.normalise` (scheme, ``.git`` suffix,
    trailing slash): also folds a schemeless ``host/org/repo`` form to the
    same shape ``normalise`` alone would leave untouched (its regexes require
    an explicit scheme or an SSH ``user@host:`` prefix to recognize input),
    and lowercases the result. Scoped to this module deliberately — the
    shared ``witan.repo`` canonicalizer preserves case for its other callers'
    exact-match graph joins; scan-overlay matching has no such join to keep
    consistent with, and GitHub repo paths are themselves case-insensitive.
    """
    if "://" not in value and "@" not in value:
        value = f"https://{value}"
    return repo_module.normalise(value).lower()


_SCAN_FIELDS = {
    "enabled": "WITAN_SCAN_ENABLED",
    "secret_action": "WITAN_SCAN_SECRET_ACTION",
    "pii_action": "WITAN_SCAN_PII_ACTION",
    "enabled_detectors": "WITAN_SCAN_ENABLED_DETECTORS",
    "disabled_detectors": "WITAN_SCAN_DISABLED_DETECTORS",
    "plugins": "WITAN_SCAN_PLUGINS",
    "allowlist": "WITAN_SCAN_ALLOWLIST",
    "allowlist_hashes": "WITAN_SCAN_ALLOWLIST_HASHES",
    "allowlist_salt": "WITAN_SCAN_ALLOWLIST_SALT",
    "on_scanner_error": "WITAN_SCAN_ON_ERROR",
}


def _scan_config_error(exc: ValidationError, sources: dict[str, str]) -> ValueError:
    """Translate a ScanConfig ValidationError into a source-attributed message."""
    err = exc.errors()[0]
    field = str(err["loc"][0])
    source = sources.get(field, field)
    if err["type"] in ("bool_parsing", "bool_type"):
        return ValueError(
            f"Invalid scan setting {source}={err['input']!r}: expected a boolean."
        )
    if err["type"] == "literal_error":
        allowed = (
            "block, warn" if field == "on_scanner_error" else "block, redact, warn"
        )
        return ValueError(
            f"Invalid scan setting {source}={err['input']!r}: expected one of {allowed}."
        )
    return ValueError(f"Invalid scan setting {source}: {err['msg']}")


def _validate_overlay(base: dict[str, object], overlay: dict[str, object]) -> None:
    """Fail loudly at load time if a ``[scan.overlay.<repo>]`` table names an
    unknown setting or produces an invalid config — surfacing that on the
    first write to the repo, deep inside ``WriteGuard``, would be far harder
    to diagnose."""
    known = set(ScanConfig.model_fields) - {"overlay"}
    for repo, overrides in overlay.items():
        if not isinstance(overrides, dict):
            raise ValueError(f"[scan.overlay.{repo!r}] must be a table.")
        bad = set(overrides) - known
        if bad:
            raise ValueError(
                f"[scan.overlay.{repo!r}] has unknown setting(s): "
                f"{', '.join(sorted(bad))}."
            )
        try:
            ScanConfig(**{**base, **overrides})
        except ValidationError as exc:
            raise ValueError(f"[scan.overlay.{repo!r}] is invalid: {exc}") from exc


def load_scan_config() -> ScanConfig:
    """Resolve ScanConfig from env > config.toml [scan] > constant defaults.

    Rejects unknown enforcement actions and non-boolean enable flags so a
    misconfigured policy fails loudly at startup rather than silently letting
    writes through unscanned. ``[scan.overlay]`` (per-repo overrides, see
    ``ScanConfig.for_repo``) has no env-var form and is read from TOML only.
    """
    file_scan = _load_toml().get("scan", {})
    if not isinstance(file_scan, dict):
        raise ValueError("The 'scan' section in config must be a table.")
    overlay = file_scan.get("overlay", {})
    if not isinstance(overlay, dict):
        raise ValueError("[scan.overlay] must be a table of repo -> settings.")

    raw: dict[str, object] = {}
    sources: dict[str, str] = {}
    for field, env_var in _SCAN_FIELDS.items():
        value = os.environ.get(env_var)
        if value is not None:
            raw[field] = value
            sources[field] = env_var
        elif field in file_scan:
            raw[field] = file_scan[field]
            sources[field] = f"[scan].{field} in config.toml"

    if overlay:
        _validate_overlay(raw, overlay)
        raw["overlay"] = overlay

    try:
        return ScanConfig(**raw)
    except ValidationError as exc:
        raise _scan_config_error(exc, sources) from exc


def default_config_toml() -> str:
    """Render a starter ``config.toml``: every optional setting present,
    commented out, shown at its actual current default (pulled from
    :class:`RankConfig`/:class:`ScanConfig` so this can't drift from the real
    defaults). Written by ``witan setup``; never overwrites an existing file.
    """
    rank = RankConfig()
    scan = ScanConfig()
    return f"""\
# witan configuration.
#
# Every setting below is optional, shown commented-out at its current
# default. Uncomment and edit to override. Resolution order (highest to
# lowest precedence): environment variable > this file > built-in default.
# See mcp/servers/witan/README.md and docs/write-path-scanning.md for the
# full reference.

# Attribution written to every graph node you create.
# Env: WITAN_AUTHOR (falls back to `git config user.name`, then $USER).
# author = "Your Name"

# Graph store location: a local path, s3://, or http(s):// URI.
# Env: WITAN_MEMORY_URI (default: ~/.local/share/witan/graph.omni)
# server = "~/.local/share/witan/graph.omni"

# Graph id addressed on an http(s):// omnigraph-server (one server serves many
# graphs). Ignored for local/s3 stores. Env: WITAN_MEMORY_GRAPH (default: council)
# graph = "council"

# Bearer token, required only for an http(s):// server.
# Env: WITAN_MEMORY_TOKEN
# token = "..."

# Default coding agent CLI for `witan run`: claude | pi | copilot | opencode | kilo
# Env: WITAN_AGENT
# agent = "claude"

# Default --model passed to the agent by `witan run`.
# Env: WITAN_MODEL
# model = "claude-opus-4-8"

# ── Named targets ────────────────────────────────────────────────────────────
# Route different repos/orgs/checkouts at different stores (e.g. work vs.
# personal). The first target whose match_paths/match_repos/match_hosts/
# match_orgs matches the current repo or local checkout wins; see the
# `load()` docstring in witan/config.py for the full precedence rules.
# witan-code reads this same file, so a target can also carry `code_dir`. A
# target can also carry remote_url/oidc_issuer/oidc_client_id/oidc_audience
# to point the CLI at a deployed witan service instead of running in-process
# (see witan_core.remote.config.RemoteConfig, ADR 0005). Those four keys route
# BOTH CLIs — `witan` and `witan-code` — since the deployment serves both tool
# surfaces on one endpoint.
#
# [targets.work]
# server = "http://witan.internal:8080"
# graph = "council"
# token = "..."
# author = "Your Name <you@corp.com>"
# agent = "claude"
# match_orgs = ["myorg"]
#
# [targets.personal]
# server = "~/.local/share/witan-personal/graph.omni"
# match_repos = ["github.com/you/dotfiles"]
# match_paths = ["~/code/personal"]
#
# [targets.hosted]
# remote_url = "https://witan.example.org/mcp"
# oidc_issuer = "https://sso.example.org/realms/ol-platform-engineering"
# match_orgs = ["ol-platform-engineering"]

# ── [rank] — memory search re-ranking ───────────────────────────────────────
# Ranking is always on; these are tuning knobs, not a feature flag. Set every
# w_* to 0 to reproduce the raw BM25 order.
[rank]
# w_bm25 = {rank.w_bm25}
# w_recency = {rank.w_recency}
# w_corrob = {rank.w_corrob}
# w_conf = {rank.w_conf}
# half_life_days = {rank.half_life_days}
# default_confidence = {rank.default_confidence}
# penalty_superseded = {rank.penalty_superseded}
# penalty_contradicted = {rank.penalty_contradicted}
# w_hop = {rank.w_hop}

# ── [scan] — write-path secret/PII scanning (ADR 0001) ──────────────────────
# Enabled by default (opt-out) — set enabled = false below to turn it off.
# See docs/write-path-scanning.md for the full guide and `witan scan rules`
# to see what's active.
[scan]
# enabled = {str(scan.enabled).lower()}
# secret_action = "{scan.secret_action}"       # block | redact | warn
# pii_action = "{scan.pii_action}"       # block | redact | warn
# enabled_detectors = []       # empty = every registered detector is active
# disabled_detectors = []      # detector names to turn off; wins over enabled_detectors
# plugins = []                 # dotted "module:Attr" paths to extra scanners
# allowlist = []               # regexes matched against a finding's own span; downgrades to audit-only
# allowlist_hashes = []        # salted sha256(allowlist_salt + span) digests of approved values
# allowlist_salt = ""          # required for allowlist_hashes to take effect
# on_scanner_error = "{scan.on_scanner_error}"       # block | warn

# Per-repo overrides (admin-owned; deliberately no WITAN_SCAN_* env-var form —
# ADR 0001 amendment 2026-07-09). Any ScanConfig field except `overlay` itself.
# [scan.overlay."github.com/example/legacy-repo"]
# secret_action = "warn"        # rolling out scanning on a noisy repo first
"""


class _Target(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    server: str | None = None
    graph: str | None = None
    token: str | None = None
    author: str | None = None
    agent: str | None = None
    model: str | None = None
    remote_url: str | None = None
    """Overrides WITAN_REMOTE_URL — routes this target's CLI commands through
    a deployed witan MCP endpoint instead of running in-process. See
    RemoteConfig/load_remote_config()."""
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_audience: str | None = None
    match_orgs: list[str] = Field(default_factory=list)
    match_repos: list[str] = Field(default_factory=list)
    match_hosts: list[str] = Field(default_factory=list)
    match_paths: list[str] = Field(default_factory=list)
    """Local checkout path prefixes (e.g. "~/code/work"). See
    witan_core.target_config.match_target for precedence — this is the most
    specific tier, checked before match_repos/match_hosts/match_orgs."""

    @field_validator(
        "match_orgs", "match_repos", "match_hosts", "match_paths", mode="before"
    )
    @classmethod
    def _normalize_match_list(cls, v: object) -> list[str]:
        return to_list(v)


def _load_toml() -> dict:
    """Load WITAN_CONFIG path or ~/.config/witan/config.toml. Returns {} on missing file."""
    return _load_toml_shared(DEFAULT_CONFIG_PATH)


def _parse_targets(raw: dict) -> list[_Target]:
    return [_Target(name=name, **cfg) for name, cfg in parse_target_tables(raw).items()]


def _resolve_path(value: str) -> str:
    """Expand ~ in local paths; leave remote URIs untouched."""
    if value.startswith(("http://", "https://", "s3://")):
        return value
    return str(Path(value).expanduser())


def _first(*values: str | None, default: str | None = None) -> str | None:
    for v in values:
        if v:
            return v
    return default


def load(target: str | None = None) -> Config:
    """Load config from file + environment, selecting a named target if applicable.

    Resolution order (highest → lowest precedence):
    1. Environment variables (WITAN_MEMORY_URI, WITAN_AGENT, WITAN_MODEL, …)
    2. Named target: ``target`` arg > WITAN_TARGET env var > auto-detect by repo
    3. Global values in config.toml
    4. Hardcoded defaults

    Each target section in config.toml can override ``server``, ``graph``,
    ``token``, ``author``, ``agent``, and ``model`` — plus, for the CLI's
    remote MCP-client mode, ``remote_url``/``oidc_issuer``/``oidc_client_id``/
    ``oidc_audience`` (see ``RemoteConfig``/``load_remote_config()``, which
    resolves those the same way). Targets are matched against the current
    repo URI detected from ``.git/config`` (or WITAN_REPO), and/or the local
    checkout path (``match_paths`` — see
    ``witan_core.target_config.match_target`` for the full precedence order).
    witan-code reads the same config.toml, so a target can carry its
    ``code_dir`` alongside these fields under one name.

    Example config.toml::

        agent = "claude"
        author = "Alice"

        [targets.work]
        server = "http://witan.internal:8080"
        graph = "council"
        token = "..."
        author = "Alice <alice@corp.com>"
        agent = "claude"
        model = "claude-opus-4-8"
        match_orgs = ["myorg"]

        [targets.personal]
        server = "~/.local/share/witan-personal/graph.omni"
        match_orgs = ["alice-personal"]
        match_repos = ["github.com/alice/dotfiles"]
        match_paths = ["~/code/personal"]

        [targets.hosted]
        remote_url = "https://witan.example.org/mcp"
        oidc_issuer = "https://sso.example.org/realms/ol-platform-engineering"
        match_orgs = ["ol-platform-engineering"]

    Raises ValueError for an explicitly-requested target that is not defined.
    """
    from . import repo as repo_module

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

    raw_server = _first(
        os.environ.get("WITAN_MEMORY_URI"),
        selected.server if selected else None,
        file_cfg.get("server"),
        default=str(_DEFAULT_GRAPH_URI),
    )

    return Config(
        graph_uri=_resolve_path(raw_server),
        graph_name=_first(
            os.environ.get("WITAN_MEMORY_GRAPH"),
            selected.graph if selected else None,
            file_cfg.get("graph"),
            default="council",
        ),
        graph_token=_first(
            os.environ.get("WITAN_MEMORY_TOKEN"),
            selected.token if selected else None,
            file_cfg.get("token"),
        ),
        author=_first(
            os.environ.get("WITAN_AUTHOR"),
            selected.author if selected else None,
            file_cfg.get("author"),
            os.environ.get("USER"),
            default="unknown",
        ),
        queries_dir=_QUERIES_DIR,
        agent=_first(
            os.environ.get("WITAN_AGENT"),
            selected.agent if selected else None,
            file_cfg.get("agent"),
            default="claude",
        ),
        model=_first(
            os.environ.get("WITAN_MODEL"),
            selected.model if selected else None,
            file_cfg.get("model"),
        ),
        target_name=selected.name if selected else None,
    )
