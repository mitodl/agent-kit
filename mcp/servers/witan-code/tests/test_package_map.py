"""Tests for the package map (witan-code.toml) and canonical symbol strings."""

from witan_code import package_map
from witan_code.bridge_extractors import ParsedBinding, canonical_symbol

REPO = "https://github.com/mitodl/mit-learn"


# ── package_map.load ──────────────────────────────────────────────


def test_load_full_declaration(tmp_path):
    (tmp_path / "witan-code.toml").write_text(
        '[package]\nname = "mit-learn"\nmanager = "pypi"\nversion = "main"\n'
        'provides = ["npm:@mitodl/course-search-utils"]\n'
    )
    identity = package_map.load(tmp_path, REPO)
    assert identity.name == "mit-learn"
    assert identity.manager == "pypi"
    assert identity.version == "main"
    assert identity.provides == ("npm:@mitodl/course-search-utils",)
    assert identity.declared


def test_load_minimal_declaration_defaults(tmp_path):
    (tmp_path / "witan-code.toml").write_text('[package]\nname = "mit-learn"\n')
    identity = package_map.load(tmp_path, REPO)
    assert identity.name == "mit-learn"
    assert identity.manager == package_map.EMPTY
    assert identity.version == "main"
    assert identity.provides == ()


def test_load_missing_file_falls_back_to_repo_basename(tmp_path):
    identity = package_map.load(tmp_path, REPO)
    assert identity.name == "mit-learn"
    assert identity.manager == package_map.EMPTY
    assert not identity.declared


def test_load_malformed_toml_falls_back(tmp_path):
    (tmp_path / "witan-code.toml").write_text("not [ valid toml ===")
    identity = package_map.load(tmp_path, REPO)
    assert identity.name == "mit-learn"
    assert not identity.declared


def test_load_missing_name_falls_back(tmp_path):
    (tmp_path / "witan-code.toml").write_text('[package]\nmanager = "pypi"\n')
    identity = package_map.load(tmp_path, REPO)
    assert not identity.declared


def test_provided_names_includes_primary_and_provides():
    identity = package_map.PackageIdentity(
        name="mit-learn",
        provides=("npm:@mitodl/course-search-utils", "bare-name"),
    )
    assert identity.provided_names() == {
        "mit-learn",
        "@mitodl/course-search-utils",
        "bare-name",
    }


# ── canonical_symbol ──────────────────────────────────────────────

IDENTITY = package_map.PackageIdentity(
    name="mit-learn", manager="pypi", version="main", declared=True
)


def _b(kind, key, key_norm, role, **kw):
    return ParsedBinding(
        kind=kind, key=key, key_norm=key_norm, role=role, file="f", **kw
    )


def test_endpoint_provider_symbol():
    b = _b("endpoint", "GET /api/v0/users/me/", "/api/v0/users/me", "provider")
    assert (
        canonical_symbol(b, IDENTITY) == "http:pypi:mit-learn:main:GET /api/v0/users/me"
    )


def test_endpoint_consumer_symbol_unresolved_with_wildcard_method():
    b = _b("endpoint", "/api/v0/users/me/", "/api/v0/users/me", "consumer")
    assert canonical_symbol(b, IDENTITY) == "http:.:.:.:* /api/v0/users/me"


def test_env_var_provider_and_consumer_symbols():
    p = _b("env_var", "MITOL_APP_BASE_URL", "MITOL_APP_BASE_URL", "provider")
    c = _b("env_var", "MITOL_APP_BASE_URL", "MITOL_APP_BASE_URL", "consumer")
    assert canonical_symbol(p, IDENTITY) == "env:pypi:mit-learn:main:MITOL_APP_BASE_URL"
    assert canonical_symbol(c, IDENTITY) == "env:.:.:.:MITOL_APP_BASE_URL"


def test_package_symbols_use_binding_key_not_repo_identity():
    p = _b("package", "@mitodl/x", "@mitodl/x", "provider", framework="npm")
    c = _b("package", "@mitodl/x", "@mitodl/x", "consumer", framework="npm")
    assert canonical_symbol(p, IDENTITY) == "pkg:npm:@mitodl/x:main:."
    assert canonical_symbol(c, IDENTITY) == "pkg:npm:@mitodl/x:.:."


def test_service_provider_symbol_includes_sub_kind():
    b = _b("service", REPO, REPO, "provider", sub_kind="repo")
    assert canonical_symbol(b, IDENTITY) == f"svc:pypi:mit-learn:main:repo/{REPO}"


def test_colon_in_identity_fields_is_escaped():
    identity = package_map.PackageIdentity(name="weird:name", manager="pypi")
    b = _b("env_var", "X_Y", "X_Y", "provider")
    assert canonical_symbol(b, identity) == "env:pypi:weird%3Aname:main:X_Y"


def test_symbol_descriptor_is_terminal_field():
    b = _b("endpoint", "GET /api/v0/users/me/", "/api/v0/users/me", "provider")
    scheme, manager, package, version, descriptor = canonical_symbol(b, IDENTITY).split(
        ":", 4
    )
    assert (scheme, manager, package, version) == ("http", "pypi", "mit-learn", "main")
    assert descriptor == "GET /api/v0/users/me"
