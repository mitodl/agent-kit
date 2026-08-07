"""Client-side config for a CLI's remote MCP-client mode (ADR 0005, path a).

Both witan-council's CLI and witan-code's CLI point at the *same* deployed
endpoint — `witan serve` mounts witan-code's `code_*` tools into the witan
FastMCP server with no prefix — so they read the same env vars
(``WITAN_REMOTE_URL`` / ``WITAN_OIDC_*``) and the same ``[targets.<name>]``
override keys, and share one token cache. Configure the deployment once and
both CLIs reach it.

The resolution *order* (env > matched target > global config.toml > default)
and the field set are identical for both, so they live here; each server keeps
only its own target selection, since that runs off its own typed target model.

A plain frozen dataclass, not a pydantic model: every value arrives as a string
from the environment or TOML, so there is nothing to coerce or validate, and
``witan_core.remote`` stays honest about depending on nothing but ``httpx2`` +
``fastmcp``. It satisfies :class:`witan_core.remote.oidc.OidcEndpoint`
structurally.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

__all__ = ["RemoteConfig", "RemoteTarget", "resolve_remote_config"]

DEFAULT_CLIENT_ID = "witan-cli"
"""Public OIDC client id both CLIs default to.

Deliberately shared: :class:`~witan_core.remote.oidc.DeviceAuth` keys its token
cache by ``(issuer, client_id)``, so one ``witan login`` also authenticates
``witan-code`` against the same deployment, and vice versa.
"""


@dataclass(frozen=True)
class RemoteConfig:
    """The client's view of a deployed witan MCP endpoint.

    Opt-in: no configured ``url`` means the CLI runs its in-process path
    exactly as before. When set, the CLI's ``_srv()`` routes read commands
    through the deployment over ``streamable-http``, authenticated with a
    per-user Keycloak JWT (device-code flow, :mod:`witan_core.remote.oidc`).
    Against a 2026-07-28 deployment that connection is stateless — no
    handshake, no session id (witan ADR-0006).

    These name the *client's* view of the deployment and are deliberately
    separate from the server-side identity config (``WITAN_ACTOR_TOKENS_FILE``
    et al.), which a CLI user never sets.
    """

    url: str
    """The deployed MCP endpoint, e.g. https://witan.example.org/mcp."""

    oidc_issuer: str
    """Keycloak realm issuer URL — where ``witan login`` discovers the device
    authorization and token endpoints."""

    oidc_client_id: str = DEFAULT_CLIENT_ID
    """Public OIDC client id registered for the device grant."""

    oidc_audience: str | None = None
    """Optional audience/resource to request, matching the deployment's
    ``WITAN_OIDC_AUDIENCE``. When set it is sent as the ``audience`` parameter
    on the device-auth and token requests (:meth:`DeviceAuth._auth_params`);
    Keycloak realms with an audience mapper honor it to stamp the ``aud`` claim
    the server validates, and realms without one ignore it harmlessly."""

    target_name: str | None = None
    """Name of the matched [targets.<name>] section that supplied any of the
    above, or None when resolved from env vars/global config.toml alone."""

    url_source: str | None = None
    """Human-readable name of the setting that actually supplied :attr:`url`,
    e.g. ``` `WITAN_REMOTE_URL` ``` or ``` `remote_url` on target [qa] ```.

    Deliberately separate from :attr:`target_name`, which says only that *a*
    target matched — not that the target is where the URL came from. Those two
    diverge in both directions: ``WITAN_REMOTE_URL`` overrides a matched
    target's ``remote_url`` (keeping ``target_name`` set), and a global
    ``remote_url`` in config.toml supplies the URL with no target matched at
    all. Anything telling a user which setting to unset to stop being routed
    remotely has to read this, or it names a key that is absent or overridden
    and the CLI stays remote after they follow the advice."""


class RemoteTarget(Protocol):
    """The remote fields a server's own ``[targets.<name>]`` model must carry.

    Structural — each server defines its own target model (they differ in the
    non-remote fields: witan's ``server``/``graph``/``token``, witan-code's
    ``code_dir``), and one target block routes both.
    """

    name: str
    remote_url: str | None
    oidc_issuer: str | None
    oidc_client_id: str | None
    oidc_audience: str | None


def _first(*values: str | None, default: str | None = None) -> str | None:
    for v in values:
        if v:
            return v
    return default


def _first_sourced(
    *candidates: tuple[str, str | None],
) -> tuple[str | None, str | None]:
    """The first truthy value, paired with the name of the setting it came from.

    Same precedence as :func:`_first`, but keeps hold of *which* source won.
    Only worth doing for ``url``: it is the setting whose presence routes the
    CLI remotely at all, so it is the one a user is told to unset.
    """
    for source, value in candidates:
        if value:
            return value, source
    return None, None


def resolve_remote_config(
    file_cfg: dict, selected: RemoteTarget | None
) -> RemoteConfig | None:
    """Build a :class:`RemoteConfig` from env > ``selected`` target > ``file_cfg``.

    ``file_cfg`` is the loaded global config.toml dict; ``selected`` the target
    block the caller matched (or None). Returns ``None`` when no ``url`` is
    configured from any source — that is in-process mode, the default.

    Raises ``ValueError`` if a URL is configured without an issuer: a remote
    endpoint the CLI can't authenticate to is useless, so fail loudly rather
    than fall through to the unauthenticated in-process path.
    """
    url, url_source = _first_sourced(
        ("`WITAN_REMOTE_URL`", os.environ.get("WITAN_REMOTE_URL")),
        (
            f"`remote_url` on target [{selected.name}]" if selected else "",
            selected.remote_url if selected else None,
        ),
        ("`remote_url` in config.toml", file_cfg.get("remote_url")),
    )
    if not url:
        return None
    issuer = _first(
        os.environ.get("WITAN_OIDC_ISSUER"),
        selected.oidc_issuer if selected else None,
        file_cfg.get("oidc_issuer"),
    )
    if not issuer:
        raise ValueError(
            "A remote witan URL is configured but no OIDC issuer is — the "
            "CLI cannot obtain a Keycloak JWT to authenticate the remote MCP "
            "connection. Set WITAN_OIDC_ISSUER (or oidc_issuer in "
            "config.toml / the matched target), and if the deployment checks "
            "it, WITAN_OIDC_AUDIENCE — or unset the remote URL."
        )
    return RemoteConfig(
        url=url,
        oidc_issuer=issuer,
        oidc_client_id=_first(
            os.environ.get("WITAN_OIDC_CLIENT_ID"),
            selected.oidc_client_id if selected else None,
            file_cfg.get("oidc_client_id"),
            default=DEFAULT_CLIENT_ID,
        ),
        oidc_audience=_first(
            os.environ.get("WITAN_OIDC_AUDIENCE"),
            selected.oidc_audience if selected else None,
            file_cfg.get("oidc_audience"),
        ),
        target_name=selected.name if selected else None,
        url_source=url_source,
    )
