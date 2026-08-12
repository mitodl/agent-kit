"""witan-code's binding of the shared MCP-client proxy (ADR 0005, path a).

The transport, argument mapping, and result-envelope unwrapping live in
:class:`witan_core.remote.proxy.RemoteMCPProxy`; :class:`RemoteServerProxy` here
binds witan-code's policy — which tools need a local checkout and so are refused
remotely (:data:`_LOCAL_ONLY`), how ``repo=None`` is resolved client-side, and
the refusal wording — so ``witan_code.cli._srv()`` gets a drop-in stand-in for
the ``witan_code.server`` module. Nothing in the read commands' call sites
changes; the deployed server does the JWT→actor mapping.
"""

from __future__ import annotations

from typing import Callable

from witan_core.remote.config import RemoteConfig
from witan_core.remote.proxy import (
    RemoteMCPProxy,
    RemoteToolFailed,
    RemoteToolUnavailable,
    RemoteUnreachable,
)

__all__ = [
    "RemoteServerProxy",
    "RemoteToolFailed",
    "RemoteToolUnavailable",
    "RemoteUnreachable",
]

# Tools that only mean anything against a local checkout. `code_reindex` IS
# registered on the deployment (it is the same server module), but running it
# there would index the *replica's* filesystem, not the caller's working tree —
# so the CLI keeps `witan-code index`/`reindex` on the in-process path and this
# refuses the remote dispatch outright rather than silently indexing nothing.
_LOCAL_ONLY = frozenset({"code_reindex"})


class RemoteServerProxy(RemoteMCPProxy):
    """Mirrors the ``witan_code.server`` tool surface, dispatching over MCP."""

    def __init__(self, cfg: RemoteConfig, token_provider: Callable[[], str]) -> None:
        super().__init__(cfg.url, token_provider)
        self._url_source = cfg.url_source

    def _is_admin_tool(self, name: str) -> bool:
        return name in _LOCAL_ONLY

    def _writes(self, name: str) -> bool:
        # Nothing witan-code dispatches remotely writes. `code_reindex` is the
        # only tool on this surface that does, and `_LOCAL_ONLY` refuses it
        # before it can ever reach a transport — so a gateway cut-off here is
        # always a read that returned nothing, never a write whose fate is
        # unknown. Stated as a rule rather than a list because it follows from
        # `_LOCAL_ONLY` above: anything that starts writing has to be added
        # there or here, and both are three lines apart.
        return False

    def _unreachable_hint(self) -> str:
        # Two ways to name the wrong setting here, and this avoids both.
        # `remote_url` is what routes a client to this proxy — NOT
        # `code_transport`, which selects the direct-omnigraph store path that
        # `store._index_locally_hint` speaks to. And *which* `remote_url` is
        # the resolver's answer to give (`url_source`), not something to infer
        # from `target_name`: env overrides a matched target, and a global key
        # supplies the URL with no target at all.
        setting = self._url_source or "the configured remote URL"
        return (
            "witan-code does not fall back to a local store — a hit-free answer "
            "from a stale or absent local index is indistinguishable from a "
            "real one, so a silent fallback would report 'no callers' for code "
            "that has them. Check the endpoint is reachable and that your "
            "session is still valid (`witan-code whoami`, then `witan-code "
            f"login`), or unset {setting} to query a locally-indexed store on "
            "purpose."
        )

    def _admin_error(self, name: str) -> str:
        return (
            f"`{name}` indexes a git checkout, which the deployed service does "
            "not have — it would index the server's own filesystem. Run "
            "`witan-code index` locally instead (indexing is always local)."
        )

    def _unknown_tool_error(self, name: str) -> str:
        return (
            f"The deployed witan service exposes no `{name}` tool. "
            "(Indexing and store maintenance run locally — see ADR-0005.)"
        )

    # NOTE: `_resolve_repo` is deliberately NOT overridden, unlike witan
    # (witan-council)'s proxy. There, every tool reads `repo=None` as "detect
    # the current repo", so the client has to substitute a value the deployed
    # server cannot derive. Here `repo=None` means "every indexed repo" on the
    # bridge-wide tools (code_precise_edges, code_unresolved_symbols,
    # code_repo_dependencies) — injecting the detected repo would silently
    # narrow `witan-code stitch` from the whole store to one repo. The commands
    # that DO want "the current repo" resolve it client-side and pass it
    # explicitly (see `witan_code.cli.symbols`).
