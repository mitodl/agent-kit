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
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from witan_core.chunking import MCP_LOAD_MAX_BYTES, chunk_records, describe_budget
from witan_core.omnigraph import store_cli_args, store_subprocess_env
from witan_core.remote.proxy import (
    RemoteCredentialRejected,
    RemoteMCPProxy,
    RemotePayloadTooLarge,
    RemoteToolFailed,
    RemoteToolUnavailable,
    RemoteUnreachable,
    RemoteWriteIndeterminate,
)

from .. import repo as repo_module
from .. import session_state
from ..config import RemoteConfig

__all__ = [
    "RemoteCredentialRejected",
    "RemotePayloadTooLarge",
    "RemoteServerProxy",
    "RemoteToolFailed",
    "RemoteToolUnavailable",
    "RemoteUnreachable",
    "RemoteWriteIndeterminate",
]


@contextmanager
def _source_export(source: str) -> Iterator[Path]:
    """Yield a path to ``source``'s export, without buffering it in memory.

    Accepts the same two shapes ``witan.server.merge_store`` does: a store URI,
    which is exported here, or an already-exported ``.jsonl``, which is used
    where it lies. The export has to happen client-side — the deployment shares
    no filesystem with the caller, which is the whole reason this path exists.

    Streams the subprocess straight to a file rather than capturing it, for the
    same reason ``_run_omnigraph`` does in-process: a real personal store's
    export is megabytes, and holding it as a string *and* as a parsed list at
    once doubles the peak for no gain.

    Unlike the in-process merge this does *not* export a target: the deployment
    reconciles against its own graph, which it already holds a client on. Only
    source rows cross the wire.
    """
    if source.startswith("file://"):
        source = source[len("file://") :]

    if source.endswith(".jsonl"):
        if source.startswith(("http://", "https://", "s3://")):
            raise RemoteToolUnavailable(
                f"{source}: a `.jsonl` source is read as an `omnigraph export` "
                "file, and witan does not fetch remote ones. Download it with "
                f"whatever already has access (e.g. `aws s3 cp {source} "
                "./export.jsonl`) and pass the local path."
            )
        if not Path(source).is_file():
            raise RemoteToolUnavailable(
                f"{source}: no such export file. A `.jsonl` source is read as "
                "an `omnigraph export`, not a store — produce one with "
                f"`omnigraph export --store <store> > {source}`."
            )
        yield Path(source)
        return

    from ..graph import OmnigraphClient

    binary = OmnigraphClient._find_binary()
    with tempfile.TemporaryDirectory(prefix="witan-remote-merge-") as tmp:
        export = Path(tmp) / "source.jsonl"
        with open(export, "w", encoding="utf-8") as fh:
            result = subprocess.run(
                [binary, "export", *store_cli_args(source)],
                stdout=fh,
                stderr=subprocess.PIPE,
                text=True,
                env=store_subprocess_env(source),
            )
        if result.returncode != 0:
            raise RemoteToolUnavailable(
                f"omnigraph export of {source} failed:\n{(result.stderr or '').strip()}"
            )
        yield export


def _merge_batch_refusal(
    exc: BaseException, *, batch: int, budget: int, dry_run: bool
) -> str:
    """Add merge-specific context to the neutral 413 refusal from witan_core.

    The base message cannot say any of this: it fires for every tool call, so
    asserting a batch budget and a half-applied write there was false for a
    refused `memory_store` and actively misleading — it told someone whose
    graph was untouched that it had been partly mutated.

    Here all three are known rather than assumed. ``batch`` is the count that
    already succeeded, so "nothing was applied" and "the merge stopped
    part-way" are distinguishable, and a ``--dry-run`` is stated as writing
    nothing at all instead of being described as a partial write.
    """
    size = describe_budget(budget)
    if dry_run:
        applied = "Nothing was written — this was a --dry-run."
    elif batch:
        applied = (
            f"The {batch} batch(es) before this one were applied: the merge "
            "stopped part-way, it did not roll back. Re-running is safe — the "
            "merge is idempotent, and rows already present are kept."
        )
    else:
        applied = "Nothing was applied: this was the first batch."
    return (
        f"{exc} This was batch {batch + 1} of the merge, sized against a budget of "
        f"{size} — so the refusal means either a single record too large to "
        f"split, or a deployment whose cap is below {size}. {applied}"
    )


def _merge_batch_indeterminate(exc: BaseException, *, batch: int, dry_run: bool) -> str:
    """Add merge-specific context to the neutral indeterminate-write message.

    The base message can only tell the caller to re-read before retrying, which
    is the right advice for a lone ``memory_store`` and needlessly frightening
    here: ``store_merge`` reconciles newest-record-wins, so re-running it is the
    remedy rather than the risk. The two facts that make that safe — how far the
    merge got, and that it is idempotent — are known only at this call site.
    """
    if dry_run:
        return (
            f"{exc} This was batch {batch + 1} of a --dry-run, which writes "
            "nothing: the interrupted call cannot have changed the graph. "
            "Re-run the dry run."
        )
    return (
        f"{exc} This was batch {batch + 1} of the merge. The {batch} batch(es) "
        "before it were applied and this one may or may not have been. Re-run "
        "`witan migrate merge` — the merge is idempotent (newest-record-wins), "
        "so a batch that did land is reconciled rather than duplicated."
    )


def _read_export(path: Path) -> list[dict]:
    """Parse an export file into load records, one line at a time.

    Materialised rather than streamed because ``chunk_records`` has to hold the
    whole set anyway — it emits every node before any edge, which cannot be
    decided without seeing all of them. Parsing per line at least avoids a
    second full copy as one big string.
    """
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RemoteToolUnavailable(
                    f"{path}: corrupted export line, not valid JSON: {line!r}"
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


# witan's read-only tool surface: the tools whose outcome after a gateway
# cut-off is uninteresting because they changed nothing.
#
# The READS are listed rather than the writes, so that the default for a tool
# nobody has classified yet is "assume it wrote" — see
# ``RemoteMCPProxy._writes``. A tool added to the server and forgotten here gets
# an over-careful message; the inverse mistake tells someone their write did not
# happen when it may have. `test_read_only_tools_are_all_registered` keeps the
# list from going stale in the other direction (an entry that no longer exists).
#
# `workflow_trace_mine` is NOT here, and is the one that looks like it should
# be: it reads sessions but writes the Memory nodes it mines out of them.
_READ_ONLY = frozenset(
    {
        "memory_for_contract",
        "memory_get",
        "memory_list",
        "memory_neighbors",
        "memory_search",
        "memory_symbols",
        "recall",
        "symbol_context",
        "task_get",
        "task_list",
        "task_ready",
        "topic_get",
        "workflow_project_get",
        "workflow_project_get_blockers",
        "workflow_project_list",
        "workflow_project_memories",
        "workflow_project_status",
        "workflow_session_list",
        "workflow_trace_get",
        "workflow_trace_list",
    }
)


# The two meanings of `repo=None`, split by what the tool does with the value.
#
# `RemoteMCPProxy._map_args` resolves an omitted `repo` client-side, because the
# deployed server has no checkout to detect one from. That is right for a tool
# that scopes a read or stamps a new row — and wrong for one that updates an
# existing row, where every parameter is "only applied if non-null" and an
# omitted `repo` means "leave it". Injecting there rewrote the stored value to
# whichever repo the caller happened to be sitting in, silently re-scoping the
# memory out of the repo it documents (#268). It reproduces only over the
# proxy: under local stdio nothing injects, and detection usually agrees anyway.
_REPO_IS_UPDATE_FIELD = frozenset({"memory_update", "task_update"})

_REPO_IS_SCOPE_OR_STAMP = frozenset(
    {
        "memory_list",
        "memory_search",
        "memory_store",
        "recall",
        "task_create",
        "task_list",
        "task_ready",
        "workflow_project_list",
        "workflow_session_start",
        "workflow_trace_list",
    }
)

# Listed only so a tool declaring `repo` cannot be added without someone
# deciding which meaning it carries — `test_every_repo_tool_is_classified`
# fails until it appears in one of the two. Neither set is consulted for
# membership at runtime: `_repo_means_detect` treats "not an update field" as
# detect, so a tool missed here still behaves as it does today.
_REPO_TOOLS = _REPO_IS_UPDATE_FIELD | _REPO_IS_SCOPE_OR_STAMP


class RemoteServerProxy(RemoteMCPProxy):
    """Mirrors the ``witan.server`` tool surface, dispatching over MCP."""

    def __init__(
        self,
        cfg: RemoteConfig,
        token_provider: Callable[[], str],
        token_refresher: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(cfg.url, token_provider, token_refresher)
        self._url_source = cfg.url_source

    def _is_admin_tool(self, name: str) -> bool:
        return name in _ADMIN_ONLY

    def _writes(self, name: str) -> bool:
        return name not in _READ_ONLY

    def _repo_means_detect(self, name: str) -> bool:
        return name not in _REPO_IS_UPDATE_FIELD

    def _unreachable_hint(self) -> str:
        # Name the setting that is actually in play, read off the resolver's
        # own record of which source won (`url_source`) rather than inferred
        # from `target_name`. A matched target does not mean the target
        # supplied the URL: `WITAN_REMOTE_URL` overrides it while leaving
        # `target_name` set, and a global `remote_url` supplies it with no
        # target at all. Inferring gets both of those backwards, and a user
        # who unsets what they were told stays routed at the dead endpoint.
        setting = self._url_source or "the configured remote URL"
        return (
            "witan does not fall back to your local store — falling back "
            "silently would split your memory across two graphs with no signal "
            "that it happened, leaving a merge nobody knew to run. Check the "
            "endpoint is reachable and that your session is still valid "
            f"(`witan whoami`, then `witan login`), or unset {setting} to work "
            "against your local store on purpose."
        )

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

        decisions: list[dict] = []
        totals = {"added": 0, "updated": 0, "kept_target": 0, "rows_loaded": 0}
        with _source_export(source) as export:
            # MCP_LOAD_MAX_BYTES, not the default: these rows ride as a JSON
            # tool parameter, so the binding ceiling is the MCP session's 4 MiB
            # body cap, not omnigraph's much larger buffered-body one.
            batches = chunk_records(_read_export(export), MCP_LOAD_MAX_BYTES)
            for index, batch in enumerate(batches):
                try:
                    result = self.store_merge(rows=batch, dry_run=dry_run)
                except RemotePayloadTooLarge as exc:
                    # Only here is the caller known to be mid-batch, so only
                    # here can the budget and the partial-write state be stated
                    # as fact rather than assumed for every tool call.
                    raise RemotePayloadTooLarge(
                        _merge_batch_refusal(
                            exc,
                            batch=index,
                            budget=MCP_LOAD_MAX_BYTES,
                            dry_run=dry_run,
                        )
                    ) from exc
                except RemoteWriteIndeterminate as exc:
                    # Same reasoning as the 413 above, opposite conclusion: the
                    # generic advice ("re-read before retrying") is wrong for a
                    # merge, which is idempotent by construction and whose
                    # remedy is simply to run it again.
                    raise RemoteWriteIndeterminate(
                        _merge_batch_indeterminate(exc, batch=index, dry_run=dry_run)
                    ) from exc
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

    def _resolve_session_id(self) -> str | None:
        # The agent session itself, for `task_claim`/`task_release`'s holder
        # qualifier. Unlike the handle above this needs no `witan session start`
        # to have run — the claim has to tell two of one person's concurrent
        # sessions apart whether or not either is attached to a project.
        return os.environ.get("CLAUDE_SESSION_ID") or None
