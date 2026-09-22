"""Serve the witan UI bundle and its runtime config from the witan process.

ONE PROCESS SERVES BOTH THE PAGE AND ``/mcp``, and that is the whole point of
putting these here rather than behind a separate static server. The page is an
MCP client (ADR 0011); served from witan's own origin it makes same-origin
calls, so there is no CORS to configure, no preflight on every tool call, and
the Host/Origin guard's same-origin rule admits the page without an allowlist.

Everything here is unauthenticated, exactly as ``/health`` is: fastmcp wraps
only the ``/mcp`` route in ``RequireAuthMiddleware``
(``fastmcp/server/http.py:620-631``). That is correct rather than an oversight.
The bundle and ``config.json`` carry no graph data, and every read the page
performs still goes through ``/mcp`` with whatever credential that requires.

The routes register only when a bundle directory exists, so a source install
that never ran the frontend build (``uv tool install``, ``pip install git+…``)
serves ``/mcp`` exactly as it does today rather than answering 404s under a
path it advertises.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from . import config as cfg_module

# Written by `npm run build` in ../ui. Its presence is what decides whether the
# routes exist at all, so it is also the thing `witan ui` checks before it
# starts a server with nothing to show.
BUNDLE_DIR = Path(__file__).parent / "ui_dist"

INDEX = "index.html"

_CLIENT_ID_VAR = "WITAN_UI_OIDC_CLIENT_ID"


def bundle_exists(bundle_dir: Path | None = None) -> bool:
    """Whether a built bundle is present to serve."""
    return ((bundle_dir or BUNDLE_DIR) / INDEX).is_file()


def _security_headers(issuer: str | None) -> dict[str, str]:
    """CSP and friends for every ``/ui/`` response.

    The page holds a token that authorizes every tool, writes included, not
    only the reads it binds. So an XSS here is a WRITE exposure, and the CSP is
    the control: no inline script, no third-party origin, nothing framed.

    ``connect-src`` has to name the issuer as well as ``'self'`` because the
    login talks directly to Keycloak's token endpoint from the page. It is
    omitted entirely when there is no issuer, which is the local case, so a
    loopback page can reach nothing but its own origin.
    """
    connect = "'self'" if not issuer else f"'self' {issuer}"
    return {
        "Content-Security-Policy": (
            f"default-src 'self'; connect-src {connect}; frame-ancestors 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
    }


# A hostname, optionally with a port. Deliberately narrow: this value is
# interpolated into a response header, and everything a legitimate Keycloak
# issuer needs is in here.
_HOSTPORT = re.compile(r"^[A-Za-z0-9.-]+(:\d{1,5})?$")


class InvalidIssuerError(ValueError):
    """``WITAN_OIDC_ISSUER`` cannot be turned into a CSP source."""


def _issuer_origin(issuer: str) -> str:
    """Scheme and host of the issuer, which is what ``connect-src`` takes.

    A CSP source is an origin; the realm path Keycloak's issuer carries
    (``/realms/ol-platform-engineering``) is not part of one and browsers
    ignore a path there, so it is dropped rather than sent and ignored.

    ★ VALIDATED, BECAUSE THIS LANDS IN A SECURITY HEADER. ``urlsplit``
    terminates ``netloc`` at ``/``, ``?`` and ``#`` but NOT at ``;`` or a
    space, so an issuer of ``https://sso.example.org; script-src *`` would
    emit a second directive and ``script-src *`` overrides ``default-src
    'self'`` for scripts. That is the whole control this CSP exists to be,
    turned off by a config value. Two quieter shapes are refused for the same
    reason rather than emitted and ignored by the browser: a scheme-less
    issuer, which produces the meaningless source ``://`` and silently blocks
    the login, and one carrying userinfo, which would publish a secret in an
    unauthenticated response header.

    Raising here means a bad value fails at startup, where whoever set it is
    looking, rather than in a browser console.
    """
    parts = urlsplit(issuer)
    if parts.scheme not in ("http", "https"):
        msg = (
            f"WITAN_OIDC_ISSUER must be an http(s) URL, got {issuer!r}. "
            "Without a scheme the page's CSP cannot name it and the login is "
            "blocked."
        )
        raise InvalidIssuerError(msg)
    if parts.username or parts.password:
        msg = (
            "WITAN_OIDC_ISSUER must not carry userinfo: it is served in an "
            "unauthenticated response header."
        )
        raise InvalidIssuerError(msg)
    if not _HOSTPORT.match(parts.netloc):
        msg = (
            f"WITAN_OIDC_ISSUER has an unusable host {parts.netloc!r}. It is "
            "interpolated into the Content-Security-Policy header, so only a "
            "hostname and optional port are accepted."
        )
        raise InvalidIssuerError(msg)
    return f"{parts.scheme}://{parts.netloc}"


def _resolve_within(bundle_dir: Path, path: str) -> Path | None:
    """Resolve ``path`` inside the bundle, or ``None`` if it escapes.

    ★ THE CHECK IS ON THE RESOLVED PATH, not on the string. Rejecting ".." in
    the input is the version of this that keeps getting written and keeps being
    bypassed (encoded separators, a symlink inside the bundle pointing out of
    it). Resolving first and then asking whether the result is still under the
    bundle answers the actual question, and covers the symlink case that no
    amount of string inspection does.
    """
    try:
        candidate = (bundle_dir / path).resolve()
    except ValueError:
        # An embedded null byte: `resolve()` raises before `is_file()` can
        # answer. Treated as "not in the bundle" rather than allowed to become
        # a 500 on an unauthenticated route that every scanner probes.
        return None
    root = bundle_dir.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def ui_config(issuer: str | None, audience: str | None, client_id: str | None) -> dict:
    """What ``/ui/config.json`` answers.

    The SPA reads this before anything else, so ONE bundle serves both modes
    with no build-time configuration: the same files that `witan ui` serves
    against a loopback store are the files the deployment serves behind a
    login. Baking the mode in at build time would mean two bundles and a way
    to get the wrong one deployed.
    """
    if not issuer:
        return {"auth": None}
    return {"auth": {"issuer": issuer, "client_id": client_id, "audience": audience}}


def register(mcp, bundle_dir: Path | None = None) -> bool:
    """Register the ``/ui/`` routes on ``mcp``. Returns whether it did.

    Via ``@mcp.custom_route``, the public API ``/health`` already uses:
    ``http_app`` takes no ``routes=`` argument
    (``fastmcp/server/mixins/transport.py:372-385``), and the alternatives are
    a private attribute or replacing ``mcp.run`` with our own uvicorn. A
    path-parameter custom route needs neither.
    """
    bundle_dir = bundle_dir or BUNDLE_DIR
    if not bundle_exists(bundle_dir):
        return False

    identity = cfg_module.load_identity_config()
    issuer = identity.oidc_issuer
    headers = _security_headers(_issuer_origin(issuer) if issuer else None)

    # Registered BEFORE the catch-all below, which would otherwise match
    # "config.json" as a bundle path and serve index.html for it.
    @mcp.custom_route("/ui/config.json", methods=["GET"])
    async def ui_config_route(_request: Request) -> Response:
        client_id = cfg_module.ui_oidc_client_id()
        if issuer and not client_id:
            return _misconfigured(headers)
        return JSONResponse(
            ui_config(issuer, identity.oidc_audience, client_id), headers=headers
        )

    @mcp.custom_route("/ui/{path:path}", methods=["GET"])
    async def ui_asset(request: Request) -> Response:
        if issuer and not cfg_module.ui_oidc_client_id():
            return _misconfigured(headers)

        path = request.path_params["path"]
        target = _resolve_within(bundle_dir, path) if path else None

        if target is None or not target.is_file():
            # Any unknown path falls back to index.html rather than 404ing, so
            # a client-side route survives a reload or a pasted link. A missing
            # ASSET would ideally still 404, but the page cannot distinguish
            # the two and this is the behaviour every SPA host has.
            target = bundle_dir / INDEX

        media_type, _ = mimetypes.guess_type(target.name)
        return FileResponse(
            target,
            media_type=media_type or "application/octet-stream",
            headers={**headers, **_cache_headers(target)},
        )

    return True


def _cache_headers(target: Path) -> dict[str, str]:
    """Cache what is content-addressed; never cache the document that names it.

    Vite gives every asset a content hash, so those are safe to keep forever.
    `index.html` is not hashed and it is what points at them: left to a
    browser's heuristic freshness (a fraction of the age since Last-Modified,
    applied when no directive is given), a client can keep an old index after a
    deploy and 404 its own bundle.
    """
    if target.name == INDEX:
        return {"Cache-Control": "no-cache"}
    return {"Cache-Control": "public, max-age=31536000, immutable"}


def _misconfigured(headers: dict[str, str]) -> JSONResponse:
    """OIDC is on and no UI client id is set.

    503 naming the variable, rather than serving a page that renders and then
    cannot log in. The failure belongs at deploy time where someone is looking,
    not in a browser console.

    Carries the same headers as every other ``/ui/`` response: "every /ui/
    response sets the CSP" is only true if the error path does too, and an
    error body is still a body a browser parses.
    """
    return JSONResponse(
        {
            "error": "witan UI is not configured",
            "detail": (
                f"{_CLIENT_ID_VAR} is unset while WITAN_OIDC_ISSUER is set, so "
                "the page has no OIDC client to log in with."
            ),
        },
        status_code=503,
        headers=headers,
    )
