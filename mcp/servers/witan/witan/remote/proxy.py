"""witan's binding of the shared MCP-client proxy (ADR 0005, path a).

The transport, argument mapping, and result-envelope unwrapping live in
:class:`witan_core.remote.proxy.RemoteMCPProxy`; :class:`RemoteServerProxy` here
binds witan's policy — which tools are in-cluster admin/break-glass ops to refuse
(:data:`_ADMIN_ONLY`), how ``repo=None`` and an omitted ``session_slug`` are
resolved client-side, and the exact refusal wording — so
``witan.cli._common._srv()`` gets a drop-in stand-in for the ``witan.server``
module. Nothing in the ~40 CLI call sites changes; the deployed server does the
ADR-0004 JWT→actor→token mapping.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

from witan_core.chunking import chunk_records
from witan_core.omnigraph import store_cli_args, store_subprocess_env
from witan_core.remote.proxy import RemoteMCPProxy, RemoteToolUnavailable

from .. import repo as repo_module
from .. import session_state
from ..config import RemoteConfig

__all__ = ["RemoteServerProxy", "RemoteToolUnavailable"]


def _export_rows(source: str) -> list[dict]:
    """The rows of ``source``, ready to ship to ``store_merge``.

    Accepts the same two shapes ``witan.server.merge_store`` does: a store URI,
    which is exported here, or an already-exported ``.jsonl``, which is read as
    it stands. The export has to happen client-side — the deployment shares no
    filesystem with the caller, which is the whole reason this path exists.

    Unlike the in-process merge this does *not* export a target: the deployment
    reconciles against its own graph, which it already holds a client on. Only
    source rows cross the wire.
    """
    if source.startswith("file://"):
        source = source[len("file://") :]

    if source.endswith(".jsonl"):
        if not Path(source).is_file():
            raise RemoteToolUnavailable(
                f"{source}: no such export file. A `.jsonl` source is read as "
                "an `omnigraph export`, not a store."
            )
        text = Path(source).read_text(encoding="utf-8")
    else:
        from ..graph import OmnigraphClient

        binary = OmnigraphClient._find_binary()
        result = subprocess.run(
            [binary, "export", *store_cli_args(source)],
            capture_output=True,
            text=True,
            env=store_subprocess_env(source),
        )
        if result.returncode != 0:
            raise RemoteToolUnavailable(
                f"omnigraph export of {source} failed:\n{(result.stderr or '').strip()}"
            )
        text = result.stdout

    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RemoteToolUnavailable(
                f"{source}: corrupted export line, not valid JSON: {line!r}"
            ) from exc
    return rows


# In-process-only module functions (deliberately not @mcp.tool): schema/
# migration/merge admin ops with no per-user identity. They belong to the
# in-cluster svc-witan-admin path (ADR-0005 path b), never the remote CLI.
#
# Not registering them is what makes them unreachable — this client-side list is
# only a better error message than the generic "no such tool" a remote dispatch
# would otherwise produce. `test_admin_only_functions_are_not_registered_as_tools`
# pins the invariant that the server keeps them off the tool surface.
#
# `merge_store` is deliberately NOT here. It is the one former member with a
# per-actor form: `RemoteServerProxy.merge_store` below exports the local store
# client-side and ships the rows through the deployment's `store_merge` tool,
# so the write is authorized as the calling user (ADR-0007 D5). The in-process
# `witan.server.merge_store` still exists for the in-cluster path and is still
# not a tool — the two are different transports for one operation, which is why
# they share a name and a call site.
_ADMIN_ONLY = frozenset(
    {
        "apply_schema",
        "migrate_topics",
        "migrate_repo_keys",
        "migrate_dedupe_sessions",
        "migrate_storage_format",
        "_topic_schema_present",
    }
)


class RemoteServerProxy(RemoteMCPProxy):
    """Mirrors the ``witan.server`` tool surface, dispatching over MCP."""

    def __init__(self, cfg: RemoteConfig, token_provider: Callable[[], str]) -> None:
        super().__init__(cfg.url, token_provider)

    def _is_admin_tool(self, name: str) -> bool:
        return name in _ADMIN_ONLY

    def _admin_error(self, name: str) -> str:
        return (
            f"`{name}` is an in-cluster admin operation, not available over the "
            "remote CLI. Run it inside the cluster as svc-witan-admin (ADR-0005 "
            "path b) — e.g. via a maintenance Job or `kubectl exec`."
        )

    def _unknown_tool_error(self, name: str) -> str:
        return (
            f"The deployed witan service exposes no `{name}` tool. "
            "(Admin/migration commands run in-cluster — see ADR-0005.)"
        )

    def merge_store(
        self, source: str, *, target: str | None = None, dry_run: bool = False
    ) -> dict:
        """Merge a local store into the deployment, as the logged-in user.

        The client half of ADR-0007 D5, and an explicit method rather than a
        ``__getattr__`` dispatch because it is not one tool call: the source
        has to be exported *here* (the deployment shares no filesystem with the
        caller) and shipped in batches. The CLI call site is identical to the
        in-process one, so ``witan migrate merge`` reads the same either way.

        ``target`` is not accepted. In-process the target is a URI the caller
        chooses; over a deployment it is that deployment's own graph, and the
        server resolves it from its own configuration — a client never names a
        store address, the same rule ADR-0005 (c) applies to witan-code's
        writes. Passing one is refused rather than ignored.
        """
        if target is not None:
            raise RemoteToolUnavailable(
                "`--target` is not accepted against a deployed witan: the "
                "target is that deployment's own graph, resolved server-side. "
                "Unset WITAN_REMOTE_URL to merge between stores you address "
                "yourself."
            )

        rows = _export_rows(source)
        decisions: list[dict] = []
        totals = {"added": 0, "updated": 0, "kept_target": 0, "rows_loaded": 0}
        for batch in chunk_records(rows):
            result = self.store_merge(rows=batch, dry_run=dry_run)
            decisions.extend(result.get("decisions") or [])
            for key in totals:
                totals[key] += result.get(key, 0)

        return {
            "dry_run": dry_run,
            "merged": not dry_run,
            "target": self._url,
            "decisions": decisions,
            **totals,
        }

    def _resolve_repo(self) -> str | None:
        return repo_module.detect()

    def _resolve_session_slug(self) -> str | None:
        # The handle `witan session start` (or the local stdio server) parked
        # under $CLAUDE_SESSION_ID. Sending it makes memories written through the
        # deployment carry SessionProduced provenance, which the server cannot
        # derive on its own — it shares neither the filesystem nor the session id.
        handle = session_state.read_handle(os.environ.get("CLAUDE_SESSION_ID") or "")
        return (handle or {}).get("session_slug") or None
