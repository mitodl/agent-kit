import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


_QUERIES_DIR = Path(__file__).parent.parent / "queries"
_DEFAULT_GRAPH_URI = Path.home() / ".local" / "share" / "witan" / "graph.omni"
_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "witan" / "config.toml"


@dataclass(frozen=True)
class Config:
    graph_uri: str
    """Local path, s3://, or http:// URI pointing at the graph."""

    graph_token: str | None
    """Bearer token. Required when graph_uri is http://. Unused for local/S3."""

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


@dataclass(frozen=True)
class _Target:
    name: str
    server: str | None
    token: str | None
    author: str | None
    agent: str | None
    model: str | None
    match_orgs: list[str]
    match_repos: list[str]
    match_hosts: list[str]


def _load_toml() -> dict:
    """Load WITAN_CONFIG path or ~/.config/witan/config.toml. Returns {} on missing file."""
    path = Path(os.environ.get("WITAN_CONFIG", str(_DEFAULT_CONFIG_PATH)))
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Failed to parse config file {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"Failed to read config file {path}: {exc}") from exc


def _to_list(val: object) -> list[str]:
    """Normalise a TOML value to a list of strings.

    Accepts a list (normal case), a bare string (convenience shorthand for a
    single-element list), or None/missing (returns []). Raises ValueError for
    anything else so config errors surface early with a clear message.
    """
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        return [str(item) for item in val]
    raise ValueError(f"Expected a list or string, got {type(val).__name__!r}")


def _parse_targets(raw: dict) -> list[_Target]:
    targets = raw.get("targets", {})
    if not isinstance(targets, dict):
        raise ValueError("The 'targets' section in config must be a table.")
    result = []
    for name, cfg in targets.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"Target {name!r} in config must be a table.")
        result.append(
            _Target(
                name=name,
                server=cfg.get("server"),
                token=cfg.get("token"),
                author=cfg.get("author"),
                agent=cfg.get("agent"),
                model=cfg.get("model"),
                match_orgs=_to_list(cfg.get("match_orgs")),
                match_repos=_to_list(cfg.get("match_repos")),
                match_hosts=_to_list(cfg.get("match_hosts")),
            )
        )
    return result


def _match_target(targets: list[_Target], repo_uri: str) -> _Target | None:
    """Return the first target whose patterns match repo_uri.

    Priority (highest first):
    1. match_repos — suffix match on host+path (e.g. "github.com/mitodl/agent-kit"
       or just "mitodl/agent-kit")
    2. match_hosts — hostname match (e.g. "github.mit.edu")
    3. match_orgs  — first path segment after host (e.g. "mitodl")
    """
    bare = re.sub(r"^https?://", "", repo_uri).rstrip("/")
    parts = bare.split("/")
    host = parts[0]
    org = parts[1] if len(parts) > 1 else ""

    for t in targets:
        for pattern in t.match_repos:
            p = re.sub(r"^https?://", "", pattern).rstrip("/")
            if bare == p or bare.endswith("/" + p):
                return t

    for t in targets:
        if host and host in t.match_hosts:
            return t

    for t in targets:
        if org and org in t.match_orgs:
            return t

    return None


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

    Each target section in config.toml can override ``server``, ``token``,
    ``author``, ``agent``, and ``model``. Targets are matched against the
    current repo URI detected from ``.git/config`` (or WITAN_REPO).

    Example config.toml::

        agent = "claude"
        author = "Alice"

        [targets.work]
        server = "http://witan.internal:8080"
        token = "..."
        author = "Alice <alice@corp.com>"
        agent = "claude"
        model = "claude-opus-4-8"
        match_orgs = ["myorg"]

        [targets.personal]
        server = "~/.local/share/witan-personal/graph.omni"
        match_orgs = ["alice-personal"]
        match_repos = ["github.com/alice/dotfiles"]

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
        repo_uri = repo_module.detect()
        selected = _match_target(targets, repo_uri) if repo_uri else None

    raw_server = _first(
        os.environ.get("WITAN_MEMORY_URI"),
        selected.server if selected else None,
        file_cfg.get("server"),
        default=str(_DEFAULT_GRAPH_URI),
    )

    return Config(
        graph_uri=_resolve_path(raw_server),
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
