"""Cross-repo interface-binding extractors for the Layer-2.5 bridge.

A *binding* is a point where a repo PROVIDES or CONSUMES a shared contract that
crosses repo boundaries: an environment variable, a shared package, a service /
deploy unit, or an HTTP endpoint. Bindings from every indexed repo accumulate in
one shared bridge store; cross-repo linkages are found by grouping on
``(kind, key_norm)``.

Extraction is HEURISTIC and syntactic — regex over source text for a handful of
well-formed, high-signal patterns, matching the same best-effort ethos as the
symbol indexer. It will miss dynamic construction and occasionally over-match.

Two tiers:
  * Tier A (``extract_file_bindings``) — per source file; rides the per-file walk
    the indexer already does. Env-var + package *consumers*, endpoint consumers.
  * Tier B (``extract_repo_bindings``) — once per repo over well-known files that
    are not otherwise indexed: OpenAPI specs (endpoint providers), Pulumi stack
    configs (env-var providers), service definitions, package.json (package
    providers).

Each consumer ``ParsedBinding`` carries a ``confidence`` score (0.0–1.0) computed
by stacking independent heuristics at extraction time.  Higher scores indicate
more likely genuine cross-repo calls; callers can filter on ``min_confidence`` to
suppress phantom edges without hardcoding library names.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

# ── Binding record ────────────────────────────────────────────────


@dataclass
class ParsedBinding:
    kind: str  # env_var | package | service | endpoint
    key: str  # raw as written (METHOD path, pkg name, env NAME)
    key_norm: str  # normalized join key
    role: str  # provider | consumer | shared
    file: str  # repo-relative source path
    sub_kind: str | None = None  # service anchor variant: repo | image | name
    symbol_id: str | None = None  # filled by the indexer (Tier A) via line lookup
    line: int | None = None
    language: str | None = None
    framework: str | None = None
    generic: bool = False
    # Confidence that this binding represents a genuine cross-repo call (0.0–1.0).
    # 1.0 for provider bindings (always genuine); computed for consumer endpoint
    # bindings by _compute_file_confidence.  Defaults to 1.0 so non-endpoint
    # consumers and providers pass through unfiltered.
    confidence: float = 1.0
    # Canonical symbol string (docs/SYMBOL_FORMAT.md), filled at bridge-write
    # time by canonical_symbol() once the repo's package identity is known.
    symbol: str | None = None


# Generic env names that appear in unrelated repos — extracted but flagged so
# cross-repo impact can de-prioritize them (a `DEBUG` edit doesn't "touch" 40 repos).
GENERIC_ENV = frozenset(
    {
        "DEBUG",
        "PORT",
        "HOST",
        "HOSTNAME",
        "PATH",
        "HOME",
        "USER",
        "ENV",
        "ENVIRONMENT",
        "LOG_LEVEL",
        "LOGLEVEL",
        "TZ",
        "LANG",
        "SECRET_KEY",
        "NODE_ENV",
        "PYTHONPATH",
        "TMPDIR",
        "PWD",
        "SHELL",
    }
)


# ── Key normalization ─────────────────────────────────────────────


def normalize_endpoint(path: str) -> str:
    """Collapse path params so producer and consumer URLs join.

    ``/api/v1/articles/{id}/`` · `` `/api/v1/articles/${x}/` `` · ``:id`` all
    normalize to ``/api/v1/articles/{}``. Strips one trailing slash.
    """
    path = path.strip().strip("`\"'")
    # Drop a leading scheme://host so consumer absolute URLs match relative paths.
    path = re.sub(r"^[a-z]+://[^/]+", "", path)
    path = re.sub(r"\$\{[^}]*\}|\{[^}]*\}|:[^/]+", "{}", path)
    path = re.sub(r"/+", "/", path)
    return path.rstrip("/") or "/"


def normalize_key(kind: str, key: str) -> str:
    if kind == "endpoint":
        # key may be "METHOD /path"; normalize only the path part.
        method, _, rest = key.partition(" ")
        if rest:
            return normalize_endpoint(rest)
        return normalize_endpoint(key)
    return key.strip()


# ── Canonical symbol strings (docs/SYMBOL_FORMAT.md) ──────────────
#
# {scheme}:{manager}:{package}:{version}:{descriptor} — five colon-separated
# fields; descriptor is terminal (parse with maxsplit=4). "." = empty/unknown.
# Providers are qualified with the repo's PackageIdentity; consumers emit
# unresolved external symbols (".") except package imports, which name their
# package at the reference site.

_SYMBOL_SCHEME = {
    "endpoint": "http",
    "env_var": "env",
    "package": "pkg",
    "service": "svc",
}

_MANAGER_BY_FRAMEWORK = {"npm": "npm", "python": "pypi"}


def _esc(field: str) -> str:
    return field.replace("%", "%25").replace(":", "%3A")


def canonical_symbol(binding: ParsedBinding, identity=None) -> str:
    """Build the canonical symbol string for ``binding``.

    ``identity`` is the repo's ``package_map.PackageIdentity``; only provider
    bindings use it (consumers stay unresolved by design).
    """
    scheme = _SYMBOL_SCHEME.get(binding.kind, binding.kind)
    provider = binding.role == "provider"

    if binding.kind == "package":
        manager = _MANAGER_BY_FRAMEWORK.get(binding.framework or "", ".")
        package = binding.key_norm
        version = identity.version if provider and identity else "."
        descriptor = "."
    else:
        manager = identity.manager if provider and identity else "."
        package = identity.name if provider and identity else "."
        version = identity.version if provider and identity else "."
        if binding.kind == "endpoint":
            method, _, rest = binding.key.strip().partition(" ")
            method = method.upper() if rest else "*"
            descriptor = f"{method} {binding.key_norm}"
        elif binding.kind == "service":
            descriptor = f"{binding.sub_kind or '.'}/{binding.key_norm}"
        else:
            descriptor = binding.key_norm

    prefix = ":".join(_esc(f) for f in (scheme, manager, package, version))
    return f"{prefix}:{descriptor}"


def _binding(kind, key, role, file, *, sub_kind=None, **kw) -> ParsedBinding:
    key_norm = normalize_key(kind, key)
    generic = kind == "env_var" and key in GENERIC_ENV
    return ParsedBinding(
        kind=kind,
        key=key,
        key_norm=key_norm,
        role=role,
        file=file,
        sub_kind=sub_kind,
        generic=generic,
        **kw,
    )


# ── Tier A: per-file consumers (regex over source text) ───────────

_PY_ENV = (
    # get_string("NAME", ...) and friends from the settings-helper convention.
    re.compile(
        r"\bget_(?:string|bool|int|float|list_of_str)\s*\(\s*[\"']([A-Z][A-Z0-9_]*[A-Z0-9])[\"']"
    ),
    # os.getenv("NAME") / os.environ.get("NAME")
    re.compile(
        r"\bos\.(?:getenv|environ\.get)\s*\(\s*[\"']([A-Z][A-Z0-9_]*[A-Z0-9])[\"']"
    ),
    # os.environ["NAME"]
    re.compile(r"\bos\.environ\s*\[\s*[\"']([A-Z][A-Z0-9_]*[A-Z0-9])[\"']"),
)

_TS_ENV = (
    # process.env.NAME / process.env["NAME"]
    re.compile(
        r"\bprocess\.env(?:\.([A-Z][A-Z0-9_]*[A-Z0-9])|\[\s*[\"']([A-Z][A-Z0-9_]*[A-Z0-9])[\"'])"
    ),
    # env("NAME") runtime accessor
    re.compile(r"\benv\s*\(\s*[\"']([A-Z][A-Z0-9_]*[A-Z0-9])[\"']"),
)

# Shared internal packages.
_PY_PKG = re.compile(
    r"\b(?:from|import)\s+(mitol\.[a-z0-9_]+)|include\(\s*[\"'](mitol\.[a-z0-9_]+)"
)
_TS_PKG = re.compile(r"(?:from|require\()\s*[\"'](@mitodl/[A-Za-z0-9._-]+)")

# Endpoint consumer path literals (string/template literals that look like API
# paths). Conservative: the value must start with "/" and contain an "api/"
# segment, to keep noise down. Matches both "/api/v1/x" and "/foo/api/x".
_TS_ENDPOINT = re.compile(r"""[\"'`](/[\w./${}:()@-]*?api/[\w./${}:()@-]*)[\"'`]""")

_PY_FRAMEWORK = "django"


# ── Confidence scoring ────────────────────────────────────────────
#
# Score starts at 0.5 (neutral).  Independent heuristics add or subtract deltas.
# Final score is clamped to [0.0, 1.0].  Only endpoint consumer bindings are
# scored; other kinds default to confidence=1.0.
#
# File-level heuristics (evaluated at extraction time, no bridge-store access):
#   generated_file      -0.8  @generated / DO NOT EDIT / openapi-generator headers
#   load_test_path      -0.6  path segment is load_testing, benchmark, perf, k6, locust
#   load_test_framework -0.6  imports k6, locust, artillery, gatling (structural pattern)
#   relative_url        -0.5  extracted URL has no scheme (no http:// or https://)
#   same_origin_helper  -0.3  relative import (./...) to csrf/fetch/api-client file
#   explicit_hostname   +0.3  extracted URL has scheme + host (absolute URL)
#   client_directory    +0.2  path segment is clients, integrations, services, api-client
#
# Store-level heuristics (applied later in bridge.py by adjust_confidence):
#   self_provided_key   -0.5  consumer repo also provides same key_norm
#   known_provider_pkg  +0.3  imported package matches a provider package slug from
#                             a different repo in the bridge store

_GENERATED_HEADERS = re.compile(
    r"@generated|Code generated by|DO NOT EDIT|openapi-generator",
    re.IGNORECASE,
)

_LOAD_TEST_PATH_SEGMENTS = frozenset(
    ["load_testing", "benchmark", "perf", "k6", "locust"]
)

# Load-test framework import patterns (name-agnostic structural detection).
# Matches: import ... from 'k6', import ... from 'k6/http', require('locust'),
# import { ... } from 'artillery', etc.
_LOAD_TEST_FRAMEWORK = re.compile(
    r"""(?:from|require\()\s*[\"'](?:k6(?:/[^\"']*)?|locust|artillery|gatling)[\"']"""
)

_HAS_SCHEME = re.compile(r"^[a-z][a-z0-9+\-.]*://")

# Same-origin helper: relative import (starts with ./ or ../) to a file whose
# basename suggests a CSRF/fetch/api-client wrapper.
_SAME_ORIGIN_RELATIVE = re.compile(
    r"""(?:from|require\()\s*[\"'](\.\.?/[^\"']*?(?:csrf|fetch|api[-_]?client|http[-_]?client)[^\"']*)[\"']""",
    re.IGNORECASE,
)

_CLIENT_PATH_SEGMENTS = re.compile(
    r"(?:^|/)(?:clients|integrations|services|api[-_]client)(?:/|$)"
)


def _compute_file_confidence(file: str, content: str, url: str) -> float:
    """Score an endpoint consumer binding using file-level heuristics only.

    Returns a value in [0.0, 1.0].  Store-dependent adjustments
    (self_provided_key, known_provider_package) are applied later by
    ``adjust_confidence`` in bridge.py after the bridge store is queried.
    """
    score = 0.5

    # Penalties
    if _GENERATED_HEADERS.search(content):
        score -= 0.8

    parts = set(file.replace("\\", "/").split("/"))
    if parts & _LOAD_TEST_PATH_SEGMENTS:
        score -= 0.6

    if _LOAD_TEST_FRAMEWORK.search(content):
        score -= 0.6

    if not _HAS_SCHEME.match(url):
        score -= 0.5

    if _SAME_ORIGIN_RELATIVE.search(content):
        score -= 0.3

    # Boosts
    if _HAS_SCHEME.match(url):
        score += 0.3

    file_lower = "/" + file.lower().replace("\\", "/")
    if _CLIENT_PATH_SEGMENTS.search(file_lower):
        score += 0.2

    return max(0.0, min(1.0, score))


def adjust_confidence(
    binding: "ParsedBinding",
    *,
    consumer_repo: str,
    provider_keys: "frozenset[tuple[str, str]]",
    has_known_provider_package: bool = False,
) -> "ParsedBinding":
    """Apply store-dependent confidence adjustments in-place and return the binding.

    Only adjusts endpoint consumer bindings; all others are returned untouched.

    ``provider_keys``            — set of ``(repo, key_norm)`` pairs for ALL
                                   provider records, including rows where
                                   repo==consumer_repo (so the self_provided_key
                                   penalty fires when the consumer repo also
                                   provides the same key_norm).
    ``has_known_provider_package``— True when the consumer file is known to import
                                   a package tracked as a provider in a different
                                   repo in the bridge store (derived from
                                   co-located package consumer bindings in the
                                   same file).
    """
    if binding.kind != "endpoint" or binding.role != "consumer":
        return binding

    score = binding.confidence

    if (consumer_repo, binding.key_norm) in provider_keys:
        score -= 0.5

    if has_known_provider_package:
        score += 0.3

    binding.confidence = max(0.0, min(1.0, score))
    return binding


def extract_file_bindings(text: str, language: str, file: str) -> list[ParsedBinding]:
    """Tier A — consumer bindings from one source file's text.

    ``symbol_id`` is left None here; the indexer fills it by line containment.

    Endpoint consumer bindings receive a ``confidence`` score computed from
    file-level heuristics (``_compute_file_confidence``).  Store-dependent
    adjustments (self_provided_key, known_provider_package) are applied later
    by ``adjust_confidence`` in bridge.py.

    All bindings are emitted regardless of their confidence score; the
    ``min_confidence`` filter in ``cross_repo_edges`` / ``build_graph`` decides
    which ones become graph edges.
    """
    out: list[ParsedBinding] = []
    if language == "python":
        for pat in _PY_ENV:
            for m in pat.finditer(text):
                name = m.group(1)
                out.append(
                    _binding(
                        "env_var",
                        name,
                        "consumer",
                        file,
                        line=_line_of(text, m.start()),
                        language=language,
                        framework=_PY_FRAMEWORK,
                    )
                )
        for m in _PY_PKG.finditer(text):
            pkg = m.group(1) or m.group(2)
            out.append(
                _binding(
                    "package",
                    pkg,
                    "consumer",
                    file,
                    line=_line_of(text, m.start()),
                    language=language,
                    framework="python",
                )
            )
    elif language in ("typescript", "javascript"):
        for pat in _TS_ENV:
            for m in pat.finditer(text):
                name = next(g for g in m.groups() if g)
                out.append(
                    _binding(
                        "env_var",
                        name,
                        "consumer",
                        file,
                        line=_line_of(text, m.start()),
                        language=language,
                        framework="nextjs",
                    )
                )
        for m in _TS_PKG.finditer(text):
            out.append(
                _binding(
                    "package",
                    m.group(1),
                    "consumer",
                    file,
                    line=_line_of(text, m.start()),
                    language=language,
                    framework="npm",
                )
            )
        for m in _TS_ENDPOINT.finditer(text):
            raw = m.group(1)
            conf = _compute_file_confidence(file, text, raw)
            b = _binding(
                "endpoint",
                raw,
                "consumer",
                file,
                line=_line_of(text, m.start()),
                language=language,
                framework="nextjs",
            )
            b.confidence = conf
            out.append(b)
    return out


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# ── Tier B: repo-level providers (well-known files) ───────────────

_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".next",
}


def extract_repo_bindings(base: Path, repo: str) -> list[ParsedBinding]:
    """Tier B — provider bindings from a repo's well-known files.

    Each sub-extractor is best-effort; a malformed file yields nothing rather
    than aborting. ``repo`` is the canonical HTTPS slug (used to self-join
    service deploy targets).
    """
    out: list[ParsedBinding] = []
    for path in _walk(base):
        rel = _rel(path, base)
        name = path.name
        try:
            if name.startswith("Pulumi.") and path.suffix in (".yaml", ".yml"):
                out.extend(_pulumi_env_vars(path, rel))
            elif name == "__main__.py":
                out.extend(_service_anchors(path, rel))
            elif name == "package.json":
                out.extend(_package_provider(path, rel))
            elif _looks_like_openapi(path):
                out.extend(_openapi_endpoints(path, rel))
        except Exception:  # noqa: BLE001 — one bad file must not abort the repo
            continue
    return out


def _walk(base: Path):
    for path in base.rglob("*"):
        if path.is_dir() or any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def _rel(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.name


def _looks_like_openapi(path: Path) -> bool:
    if path.suffix not in (".json", ".yaml", ".yml"):
        return False
    if path.name.lower() in (
        "openapi.json",
        "openapi.yaml",
        "openapi.yml",
        "schema.yaml",
    ):
        return True
    return "openapi" in path.parts


def _pulumi_env_vars(path: Path, rel: str) -> list[ParsedBinding]:
    """Env-var providers: keys of any `<project>:env_vars` map in a stack config."""
    import yaml

    data = yaml.safe_load(path.read_text()) or {}
    config = data.get("config", data) if isinstance(data, dict) else {}
    out: list[ParsedBinding] = []
    for cfg_key, value in (config or {}).items():
        if cfg_key.endswith(":env_vars") and isinstance(value, dict):
            for env_name in value:
                if isinstance(env_name, str):
                    out.append(
                        _binding(
                            "env_var", env_name, "provider", rel, framework="pulumi"
                        )
                    )
    return out


_SVC_REPO = re.compile(
    r"github\.get_repository\(\s*full_name\s*=\s*[\"']([^\"']+)[\"']"
)
_SVC_IMAGE = re.compile(r"application_image_repository\s*=\s*[\"']([^\"']+)[\"']")
_SVC_NAME = re.compile(r"application_name\s*=\s*[\"']([^\"']+)[\"']")


def _service_anchors(path: Path, rel: str) -> list[ParsedBinding]:
    """Service/deploy anchors from an ol-infrastructure application __main__.py.

    The repo full_name is normalized to its canonical HTTPS URI so it joins
    against the deployed repo's own slug (kind=service, key_norm=<that URI>).
    sub_kind ("repo" | "image" | "name") distinguishes anchor types without
    packing the variant into the key string — joining on (kind, key_norm) works
    even when the consumer records only the bare URI/name.
    """
    text = path.read_text()
    out: list[ParsedBinding] = []
    for m in _SVC_REPO.finditer(text):
        raw = m.group(1)
        uri = f"https://github.com/{raw}" if "/" in raw and "://" not in raw else raw
        out.append(
            _binding(
                "service",
                uri,
                "provider",
                rel,
                sub_kind="repo",
                line=_line_of(text, m.start()),
                framework="pulumi",
            )
        )
    for m in _SVC_IMAGE.finditer(text):
        out.append(
            _binding(
                "service",
                m.group(1),
                "provider",
                rel,
                sub_kind="image",
                line=_line_of(text, m.start()),
                framework="pulumi",
            )
        )
    for m in _SVC_NAME.finditer(text):
        out.append(
            _binding(
                "service",
                m.group(1),
                "provider",
                rel,
                sub_kind="name",
                line=_line_of(text, m.start()),
                framework="pulumi",
            )
        )
    return out


def _package_provider(path: Path, rel: str) -> list[ParsedBinding]:
    """A repo that publishes an @mitodl/* package provides it."""
    data = json.loads(path.read_text())
    name = data.get("name")
    if isinstance(name, str) and name.startswith("@mitodl/"):
        return [_binding("package", name, "provider", rel, framework="npm")]
    return []


_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def _openapi_endpoints(path: Path, rel: str) -> list[ParsedBinding]:
    """Endpoint providers: every path+method in a drf-spectacular OpenAPI spec."""
    import yaml

    raw = path.read_text()
    data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
    if not isinstance(data, dict) or "paths" not in data:
        return []
    out: list[ParsedBinding] = []
    for url, methods in (data.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method in methods:
            if method.lower() in _HTTP_METHODS:
                key = f"{method.upper()} {url}"
                out.append(_binding("endpoint", key, "provider", rel, framework="drf"))
    return out
