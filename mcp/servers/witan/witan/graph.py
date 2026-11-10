"""witan's OmnigraphClient — the shared base plus witan's own tail.

The subprocess/lock/retry/admission-cap machinery lives in
``witan_core.omnigraph``; this subclass adds witan's ``apply_schema`` command
and the storage-version friendly-error remediation hint (the write ``guard`` +
``surface_conflict`` support is already on the base).

``OmnigraphConflict``, ``StoreUnavailable`` and ``_is_storage_version_mismatch``
are re-exported for ``server.py`` (and its migrate helpers), which import them
from here.
"""

from __future__ import annotations

from witan_core.omnigraph import (
    _SERVER_SCHEMES,
    OmnigraphConflict,
    StoreUnavailable,
    _is_storage_version_mismatch,
)
from witan_core.omnigraph import (
    OmnigraphClient as _BaseOmnigraphClient,
)

__all__ = [
    "OmnigraphClient",
    "OmnigraphConflict",
    "StoreUnavailable",
    "_is_storage_version_mismatch",
]


class OmnigraphClient(_BaseOmnigraphClient):
    """The base client, specialized for witan (memory/work-coordination store)."""

    _SETUP_HINT = "witan setup"
    _STORAGE_MISMATCH_HINT = (
        "Run `witan migrate storage` to move this store to the "
        "currently installed omnigraph version's format."
    )

    def apply_schema(self, schema_path) -> str:
        """Apply a schema file to the store (idempotent). Returns CLI stdout.

        Runs through the same per-store write lock + retry/repair as a mutation,
        so it can't race other writers and leave the store drifted. Uses a raw
        ``_execute`` (not ``_run``) because ``schema apply`` is a two-word
        subcommand and takes none of ``_run``'s injected args.

        Refuses a served graph before spawning anything. Since 0.13 the CLI
        rejects ``schema apply`` against a server ("requires a standalone
        storage URI"): a served graph belongs to a cluster, and a cluster
        graph's schema changes only through ``cluster apply``, which records it
        in the deployment ledger. Without this check a 0.14+ CLI fails earlier
        on an unrelated complaint (plain HTTP to a non-loopback server), which
        sends an operator off to set up TLS for a command that would still be
        refused.
        """
        if self.graph_uri.startswith(_SERVER_SCHEMES):
            raise RuntimeError(
                f"{self.graph_uri} is a served graph, so `schema apply` cannot "
                "change it: omnigraph changes a served graph's schema only "
                "through `cluster apply`. The server runs that apply against "
                "the schema baked into its image every time it starts, so "
                "deploy the omnigraph-server image that carries the new "
                "schema.pg, or restart omnigraph-server to re-run the apply. "
                "(`omnigraph cluster apply --target <name>` also works when "
                "<name> is a configured server target with an https:// "
                "endpoint.)"
            )
        cmd = [
            self._binary,
            "schema",
            "apply",
            "--schema",
            str(schema_path),
            *self._store_args(),
        ]
        return self._execute(cmd, "schema apply", is_write=True)
