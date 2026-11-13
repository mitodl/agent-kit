"""What a token issued to the web UI's own Keycloak client may call.

The page only reads, through ADR 0011 §3's bound set. Its access token is an
ordinary ``aud: witan`` token, though, and witan's ``JWTVerifier`` checks only
issuer, audience and expiry, so on its own that token authorizes every tool,
writes included. A script injected into the page could then write to or delete
from the shared graph as the person viewing it.

Keycloak stamps the client a token was issued to into ``azp``, which fastmcp
surfaces as ``AccessToken.client_id`` (it prefers a ``client_id`` claim when
one exists; the UI client's user tokens carry none). A token whose client is the UI's
(``WITAN_UI_OIDC_CLIENT_ID``, the same value ``/ui/config.json`` hands the
page) is limited here to :data:`READ_TOOLS`. Every other client (``witan-cli``,
``witan-desktop``) is untouched, and so is local ``witan ui``, which sends no
token at all.

Keyed on the client rather than on a Keycloak scope because the client is
already distinguishable and nothing outside witan has to change. A scope-based
gate (a ``witan:write`` default scope on every client but the UI's) would work
too, but needs a realm change deployed before the witan release that requires
it, and a second copy of which tools count as writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp.utilities.authorization import AuthCheck, AuthContext

#: ADR 0011 §3's bound set: the whole read surface the UI may call. Adding a
#: tool here means amending the ADR first. ``bin/gen_ui_fixtures.py`` records
#: the page's fixtures from this set, so the page and this gate cannot disagree.
READ_TOOLS = frozenset(
    {
        "task_ready",
        "task_get",
        "task_list",
        "task_search",
        "workflow_project_get",
        "workflow_project_status",
        "workflow_project_list",
        "workflow_session_list",
        "recall",
        "memory_get",
        "memory_list",
        "memory_search",
        "memory_contradictions",
        "code_repo_dependencies",
        "code_interface_providers",
        "code_interface_consumers",
    }
)

#: Bound tools a server may legitimately lack: witan-code is a separate
#: package, mounted only when installed (ADR 0011, 2026-09-23 amendment).
OPTIONAL_READ_TOOLS = frozenset(
    {"code_repo_dependencies", "code_interface_providers", "code_interface_consumers"}
)


def reads_only_for(ui_client_id: str | None) -> AuthCheck:
    """An ``AuthMiddleware`` check limiting ``ui_client_id``'s tokens to reads.

    Resources and prompts pass: the only resources are the ``ui://`` widgets,
    which are static bundle files. ``AuthMiddleware`` applies the check to
    ``tools/list`` as well as ``tools/call``, so the page is also shown only
    the tools it may call.

    :param ui_client_id: The UI's Keycloak client id, or ``None`` when this
        deployment serves no UI, in which case every token passes.
    :returns: The check.
    """

    def check(ctx: AuthContext) -> bool:
        if ui_client_id is None or ctx.token is None:
            return True
        if ctx.token.client_id != ui_client_id:
            return True
        tool = ctx.tool
        return tool is None or tool.name in READ_TOOLS

    return check
