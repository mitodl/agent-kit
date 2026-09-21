"""Serving the UI bundle and its config from the witan process.

Four properties, each with a failure that is invisible from the other tests:

  * the bundle is reachable and a client-side route survives a reload;
  * nothing OUTSIDE the bundle directory is reachable through it;
  * `/ui/config.json` is not shadowed by the catch-all, and says which mode
    the page is in without the page having been built for that mode;
  * with OIDC on and no UI client id, `/ui/` refuses rather than serving a
    page that renders and then cannot log in.

The routes are built against a temporary bundle rather than the real
`witan/ui_dist`, which is a build output the test run has no reason to have.
"""

import json

import pytest
from fastmcp import FastMCP
from starlette.testclient import TestClient
from witan import ui_routes


@pytest.fixture
def bundle(tmp_path):
    """A stand-in for what `npm run build` writes."""
    root = tmp_path / "ui_dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>witan</title>")
    (root / "assets" / "app.js").write_text("export const x = 1;\n")
    (root / "assets" / "app.css").write_text(".a{}\n")
    return root


def _client(bundle, monkeypatch, **env):
    """A server with the UI routes mounted on the given bundle."""
    for key in (
        "WITAN_OIDC_ISSUER",
        "WITAN_OIDC_AUDIENCE",
        "WITAN_OIDC_RESOURCE_URL",
        "WITAN_ACTOR_TOKENS_FILE",
        "WITAN_UI_OIDC_CLIENT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    mcp = FastMCP("witan-ui-test")
    assert ui_routes.register(mcp, bundle) is True
    return TestClient(mcp.http_app(path="/mcp"))


def _oidc_env(tmp_path, **extra):
    """The four vars `load_identity_config` insists on together, plus extras."""
    tokens = tmp_path / "actors.json"
    tokens.write_text("{}")
    return {
        "WITAN_OIDC_ISSUER": "https://sso.example.org/realms/ol-platform-engineering",
        "WITAN_OIDC_AUDIENCE": "witan",
        "WITAN_OIDC_RESOURCE_URL": "https://witan.example.org",
        "WITAN_ACTOR_TOKENS_FILE": str(tokens),
        **extra,
    }


# ── The bundle is reachable ────────────────────────────────────────


def test_the_index_is_served(bundle, monkeypatch):
    response = _client(bundle, monkeypatch).get("/ui/")

    assert response.status_code == 200
    assert "<title>witan</title>" in response.text


def test_an_asset_is_served_with_its_own_media_type(bundle, monkeypatch):
    """`nosniff` is set on every response, so a wrong type is a broken page
    rather than something the browser quietly recovers from."""
    client = _client(bundle, monkeypatch)

    js = client.get("/ui/assets/app.js")
    css = client.get("/ui/assets/app.css")

    assert js.status_code == 200
    assert js.headers["content-type"].startswith("text/javascript")
    assert css.headers["content-type"].startswith("text/css")


def test_an_unknown_path_falls_back_to_the_index(bundle, monkeypatch):
    """A client-side route has to survive a reload and a pasted link."""
    response = _client(bundle, monkeypatch).get("/ui/board/tk-something")

    assert response.status_code == 200
    assert "<title>witan</title>" in response.text


# ── Nothing outside the bundle is reachable ────────────────────────


# Two different refusals, and which one a case gets is the point: a literal
# `..` is normalized away by the client so the router never matches and 404s,
# while an encoded or absolute path reaches the handler and the containment
# check falls it back to the index. Pinning the status per case is what keeps
# the second group honest. Accepting either everywhere would let all four
# collapse into router 404s (if httpx or Starlette started decoding `%2f`)
# with every assertion still green and `_resolve_within` untested.
ROUTER_404 = 404
HANDLER_FALLBACK = 200


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("../../../../etc/passwd", ROUTER_404),
        ("assets/../../../../etc/passwd", ROUTER_404),
        ("..%2f..%2f..%2fetc%2fpasswd", HANDLER_FALLBACK),
        ("/etc/passwd", HANDLER_FALLBACK),
        # An embedded null byte reaches `resolve()`, which raises before
        # `is_file()` can answer. Uncaught it is a 500 on an unauthenticated
        # route that every scanner probes.
        ("a%00b", HANDLER_FALLBACK),
    ],
)
def test_a_path_escaping_the_bundle_is_refused(bundle, monkeypatch, path, expected):
    """★ None of these may read a file outside the bundle, or 500.

    The resolved path is what the handler checks, not the string; rejecting
    `..` in the input is the version that keeps getting bypassed.
    """
    response = _client(bundle, monkeypatch).get(f"/ui/{path}")

    assert response.status_code == expected
    assert "root:" not in response.text
    if expected == HANDLER_FALLBACK:
        assert "<title>witan</title>" in response.text


def test_a_symlink_out_of_the_bundle_is_refused(bundle, monkeypatch, tmp_path):
    """The case string inspection cannot catch: a path with no "..'" in it.

    `resolve()` follows the link, so the containment check sees where it
    actually lands.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("do-not-serve")
    (bundle / "escape.txt").symlink_to(secret)

    response = _client(bundle, monkeypatch).get("/ui/escape.txt")

    # The status and the body identity, not just the absence of the secret:
    # "do-not-serve" is also absent from a 500 and from an empty body, so this
    # is the one test uniquely covering resolve-through-symlink and it has to
    # pin what actually came back.
    assert response.status_code == 200
    assert "<title>witan</title>" in response.text
    assert "do-not-serve" not in response.text


def test_a_symlink_inside_the_bundle_still_resolves(bundle, monkeypatch):
    """The negative control: containment must not reject a legitimate link."""
    (bundle / "alias.js").symlink_to(bundle / "assets" / "app.js")

    response = _client(bundle, monkeypatch).get("/ui/alias.js")

    assert response.status_code == 200
    assert "export const x = 1;" in response.text


def test_a_directory_symlink_out_of_the_bundle_is_refused(
    bundle, monkeypatch, tmp_path
):
    """A linked DIRECTORY escapes just as a linked file does."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("do-not-serve")
    (bundle / "linked").symlink_to(outside, target_is_directory=True)

    response = _client(bundle, monkeypatch).get("/ui/linked/secret.txt")

    assert response.status_code == 200
    assert "do-not-serve" not in response.text


# ── config.json ────────────────────────────────────────────────────


def test_config_json_is_not_shadowed_by_the_catch_all(bundle, monkeypatch):
    """Registration order is what keeps this JSON rather than index.html."""
    response = _client(bundle, monkeypatch).get("/ui/config.json")

    assert response.status_code == 200
    assert json.loads(response.text) == {"auth": None}


def test_config_json_reports_the_deployed_auth_mode(bundle, monkeypatch, tmp_path):
    """One bundle serves both modes; this is how the page tells them apart."""
    env = _oidc_env(tmp_path, WITAN_UI_OIDC_CLIENT_ID="witan-ui")

    response = _client(bundle, monkeypatch, **env).get("/ui/config.json")

    assert response.json() == {
        "auth": {
            "issuer": "https://sso.example.org/realms/ol-platform-engineering",
            "client_id": "witan-ui",
            "audience": "witan",
        }
    }


def test_oidc_on_with_no_client_id_refuses(bundle, monkeypatch, tmp_path):
    """503 naming the variable, on the page AND on the config.

    Serving a page that renders and then cannot log in puts the failure in a
    browser console instead of in front of whoever deployed it.
    """
    client = _client(bundle, monkeypatch, **_oidc_env(tmp_path))

    page = client.get("/ui/")
    config = client.get("/ui/config.json")

    assert page.status_code == 503
    assert config.status_code == 503
    assert "WITAN_UI_OIDC_CLIENT_ID" in config.text
    # "Every /ui/ response sets the CSP" is only true if the error path does.
    for response in (page, config):
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"


# ── Security headers ───────────────────────────────────────────────


def test_every_ui_response_carries_the_csp_and_nosniff(bundle, monkeypatch):
    """The token the page holds authorizes writes, so an XSS here is a write
    exposure and the CSP is the control."""
    client = _client(bundle, monkeypatch)

    for path in ("/ui/", "/ui/assets/app.js", "/ui/config.json"):
        response = client.get(path)
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers["x-content-type-options"] == "nosniff"


def test_the_local_csp_names_no_third_party_origin(bundle, monkeypatch):
    """With no issuer there is nothing to talk to but our own origin."""
    response = _client(bundle, monkeypatch).get("/ui/")

    assert "connect-src 'self';" in response.headers["content-security-policy"]


def test_the_deployed_csp_allows_the_issuer_origin_only(bundle, monkeypatch, tmp_path):
    """The login posts to Keycloak's token endpoint from the page, so the
    issuer has to be in `connect-src`. Its realm PATH must not be: a CSP
    source is an origin, and a browser ignores the path."""
    env = _oidc_env(tmp_path, WITAN_UI_OIDC_CLIENT_ID="witan-ui")

    response = _client(bundle, monkeypatch, **env).get("/ui/")

    csp = response.headers["content-security-policy"]
    assert "connect-src 'self' https://sso.example.org;" in csp
    assert "realms" not in csp


@pytest.mark.parametrize(
    "issuer",
    [
        "https://sso.example.org; script-src *; default-src *",
        "sso.example.org/realms/ol",
        "https://svc:s3cret@sso.example.org/realms/ol",
        # A space INSIDE the netloc (no slash), which urlsplit keeps. With a
        # slash the space lands in the path and is dropped, which is safe.
        "https://sso.example.org realms",
        "https://sso.example.org*",
    ],
)
def test_an_unusable_issuer_is_refused_at_registration(
    bundle, monkeypatch, tmp_path, issuer
):
    """★ THE ISSUER LANDS IN A SECURITY HEADER, so it is validated there.

    `urlsplit` ends `netloc` at `/`, `?` and `#` but not at `;` or a space, so
    an issuer carrying `; script-src *` emits a second CSP directive and
    `script-src *` overrides `default-src 'self'` for scripts. That is the
    control this CSP exists to be, disabled by a config value.

    The quieter shapes fail here too rather than being emitted and ignored: a
    scheme-less issuer yields the meaningless source `://` and silently blocks
    the login, and userinfo would publish a secret in an unauthenticated
    response header.
    """
    env = _oidc_env(tmp_path, WITAN_UI_OIDC_CLIENT_ID="witan-ui")
    env["WITAN_OIDC_ISSUER"] = issuer

    with pytest.raises(ui_routes.InvalidIssuerError):
        _client(bundle, monkeypatch, **env)


def test_a_legitimate_issuer_with_a_port_is_accepted(bundle, monkeypatch, tmp_path):
    """The validation must not reject a real deployment shape."""
    env = _oidc_env(tmp_path, WITAN_UI_OIDC_CLIENT_ID="witan-ui")
    env["WITAN_OIDC_ISSUER"] = "https://sso.example.org:8443/realms/ol"

    response = _client(bundle, monkeypatch, **env).get("/ui/")

    csp = response.headers["content-security-policy"]
    assert "connect-src 'self' https://sso.example.org:8443;" in csp


# ── Caching ────────────────────────────────────────────────────────


def test_the_index_is_not_cached_but_hashed_assets_are(bundle, monkeypatch):
    """★ The index names the hashed assets, so caching it is what breaks a deploy.

    With no directive a browser applies heuristic freshness, and a client that
    keeps yesterday's index after a deploy 404s the bundle it points at.
    """
    client = _client(bundle, monkeypatch)

    index = client.get("/ui/")
    asset = client.get("/ui/assets/app.js")

    assert index.headers["cache-control"] == "no-cache"
    assert "immutable" in asset.headers["cache-control"]


# ── Registration is conditional ────────────────────────────────────


def test_no_bundle_means_no_routes(tmp_path):
    """★ A source install that never ran the frontend build must serve `/mcp`
    exactly as it does today, rather than answering 404 under a path it
    advertises. `uv tool install` and `pip install git+…` are both this case.
    """
    mcp = FastMCP("witan-ui-absent")

    assert ui_routes.register(mcp, tmp_path / "nothing") is False

    with TestClient(mcp.http_app(path="/mcp")) as client:
        assert client.get("/ui/").status_code == 404
