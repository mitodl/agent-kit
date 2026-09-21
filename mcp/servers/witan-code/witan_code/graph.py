"""witan-code's OmnigraphClient — the shared base plus witan-code's own tail.

The subprocess/lock/retry/admission-cap machinery lives in
``witan_core.omnigraph``; this subclass adds omnigraph *branch* support (each
per-user/per-session code index is isolated on its own store branch) and the
bulk ``load`` used to write thousands of symbol/edge records in one call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from witan_core.omnigraph import OmnigraphClient as _BaseOmnigraphClient
from witan_core.omnigraph import OmnigraphConflict, _is_storage_version_mismatch

from witan_core import chunking
from . import config as cfg_module
from . import identity as identity_module
from . import views

__all__ = [
    "OmnigraphClient",
    "SharedGraphWriteRefused",
    "UnquotableBranchName",
    "check_writable",
    "is_stale_schema",
    "owns_view",
]

# Re-exported under a public name because witan-code has a second, non-error
# use for it: `store.store_health` classifies a store it probed deliberately,
# not an exception that escaped. Same detector either way — a second copy of
# the marker pair would drift.
is_stale_schema = _is_storage_version_mismatch


class SharedGraphWriteRefused(RuntimeError):
    """A writer tried to write a view of a shared graph it does not own."""


class UnquotableBranchName(RuntimeError):
    """A branch name that cannot be written into a GQ branch statement.

    A ``RuntimeError`` on purpose: the names that reach :func:`_gq_branch_name`
    from outside this package come from ``branch list``, so the reaper meets
    them one at a time and must record the bad one in ``report.failed`` and
    carry on. Its handler catches ``RuntimeError``, and a sibling type would
    abort the whole sweep at the oldest poison name, stranding every newer
    stale view behind it.
    """


#: What a branch name may contain for :func:`_gq_branch_name` to be able to put
#: it in a statement. Deliberately the ENGINE'S rule, not witan-code's: 0.11's
#: storage layer validates each ``/``-separated segment as "alphanumeric, '.',
#: '-', '_'", and its "alphanumeric" is Unicode, so ``café`` and ``act-x/дом``
#: are both branch names it creates quite happily. ``\w`` is that same set
#: (Unicode word characters, which include ``_``), plus the ``.``, ``-`` and
#: the ``/`` that separates segments.
#:
#: Matching the engine rather than our own sanitizers is what keeps this a
#: quoting guard instead of a second, stricter naming policy: every name
#: witan-code builds is inside it either way, and a name some other client
#: created is refused only when the engine would refuse it too.
_QUOTABLE_BRANCH = re.compile(r"\A[\w./-]+\Z")

#: How many times :meth:`OmnigraphClient.ensure_branch` re-lists and retries
#: after a 409. Small on purpose: the duplicate-create case resolves on the
#: second listing, and the transient precondition it also covers already has
#: the engine's own backoff underneath each attempt.
_ENSURE_BRANCH_ATTEMPTS = 3


def _gq_branch_name(name: str) -> str:
    """``name`` as a GQ string literal, or raise :class:`UnquotableBranchName`.

    A branch statement needs its name QUOTED. Verified against the 0.11.0
    binary: ``branch create act-x/feat_y from main`` is a ``parse error``, and
    ``branch create "act-x/feat_y" from main`` succeeds.

    GQ *does* honour ``\\"`` inside the quotes, so the danger is not that a
    ``"`` fails to parse. It is that it parses fine and the statement then
    means something other than what the caller asked. What refuses
    ``"we\\"ird"`` is the storage layer, one level down and after the parse:
    ``storage: Ref is invalid: Branch segment 'we"ird.<ulid>' contains invalid
    characters``. So the charset above, which excludes both ``"`` and ``\\``,
    is what keeps a name from reaching that point at all.

    Unreachable for a name this package built. The ones that do not come from
    here are whatever ``branch list`` returns, which is whatever any client
    ever created on the store.
    """
    if not _QUOTABLE_BRANCH.match(name):
        raise UnquotableBranchName(
            f"branch name {name!r} cannot be written as an omnigraph GQ "
            r"statement: it has characters outside [\w./-], which is what "
            "omnigraph itself allows in a branch segment."
        )
    return f'"{name}"'


def owns_view(
    *,
    is_remote: bool,
    branch: str | None,
    cfg: cfg_module.Config,
    actor: str | None,
) -> bool:
    """Whether this process is the single writer of the view it is about to touch.

    The one question both destructive operations turn on — writing a view, and
    purging rows from one — so they ask it in the same place rather than each
    spelling out its own approximation of it. Three cases:

    - **A local store has one user, who is its writer.** Nothing to arbitrate.
    - **CI owns the shared default view.** ``branch is None`` means the write
      targets the store's ``main``, the view every reader falls back to. On the
      cluster that view has exactly one writer, the CI indexer, and it says so
      (``WITAN_CODE_INDEX_ROLE=ci``). Authority is the declared role, never the
      transport: CI is remote too, so "refuse when remote" would block the one
      writer the design depends on.
    - **Each actor owns its own branch views.** Per-user branch views live ON
      the shared graph (DECIDED, Tobias 2026-07-31) — in-flight work being
      visible to other agents as it happens is much of what the shared service
      is for — so isolation cannot come from *where* the view lives. It comes
      from the name: a view is owned by the actor it is prefixed with
      (:mod:`witan_code.views`), and a process writes only views prefixed with
      its own. An un-prefixed branch view on a shared graph is owned by nobody,
      which is the collision this replaced.
    """
    if not is_remote:
        return True
    if branch is None:
        return cfg.is_designated_writer
    return actor is not None and views.owner(branch) == actor


def check_writable(
    *,
    is_remote: bool,
    branch: str | None,
    cfg: cfg_module.Config,
    slug: str,
    actor: str | None,
) -> None:
    """Raise :class:`SharedGraphWriteRefused` unless :func:`owns_view` allows it.

    ``actor`` is the identity the write is being made as, and ``None`` means
    exactly one thing: there is no identity to own the view. Required, with no
    fallback to :func:`~witan_code.identity.actor_id` — every caller already
    resolves an actor, because it needs one to *name* the view it is about to
    write, and a caller serving somebody else's write resolves theirs, not this
    process's (:mod:`witan_code.ingest`). A default would make ``None`` mean
    "logged out" or "ask the machine" depending on who was calling, and the
    second reading is the one that cannot be right here.

    ``is_remote`` is "is this graph shared", which for a client is a property
    of its store (``client.is_remote``) and for the MCP tier serving somebody
    else's write is true by construction — that is the only reason the request
    exists (:mod:`witan_code.ingest`). Taking the bit rather than the client is
    what lets both ask the same question.
    """
    if owns_view(is_remote=is_remote, branch=branch, cfg=cfg, actor=actor):
        return
    if branch is None:
        raise SharedGraphWriteRefused(
            f"Refusing to index {slug} onto the shared graph's default branch: "
            f"that view is owned by CI, and this process is acting as "
            f"'{cfg.index_role}'. Index a non-default git branch to write an "
            f"isolated branch view, or set "
            f"WITAN_CODE_INDEX_ROLE={cfg_module.INDEX_ROLE_CI} if this IS the "
            f"CI indexer."
        )
    if actor is None:
        raise SharedGraphWriteRefused(
            f"Refusing to write branch view {branch!r} of {slug} to a shared "
            "graph without an identity to own it: another writer on the same "
            "git branch would silently overwrite it. Run `witan login`, or set "
            f"{identity_module.ACTOR_ENV_VAR} for a non-interactive writer."
        )
    raise SharedGraphWriteRefused(
        f"Refusing to write branch view {branch!r} of {slug}: it is owned by "
        f"{views.owner(branch) or 'nobody'}, and this process is {actor}. "
        "Branch views are readable by everyone and writable only by their "
        "owner."
    )


class OmnigraphClient(_BaseOmnigraphClient):
    """The base client, specialized for witan-code (per-repo code-graph stores)."""

    _SETUP_HINT = "witan-code setup (or `witan setup`, if witan is also installed)"
    # Deliberately NOT witan's `migrate storage` advice. That path exports with
    # the old binary and reloads with the new one, because a memory graph holds
    # the only copy of what it knows. A code graph does not: it is derived from
    # a checkout, and reindexing rebuilds it from source without needing the
    # pre-upgrade binary to still be installed
    # (tk-rebuild-derived-graphs-by-reindexing-not-by-expo-3b781b).
    _STORAGE_MISMATCH_HINT = (
        "This code graph was written by an older omnigraph and the installed "
        "one cannot open it. A code graph is derived from the checkout, so it "
        "is rebuilt by reindexing rather than migrated: run `witan-code "
        "doctor` for every affected store, then `witan-code reindex --rebuild` "
        "in each affected checkout."
    )

    def __init__(
        self,
        graph_uri: str,
        queries_dir: Path,
        token: str | None = None,
        branch: str | None = None,
        graph_id: str | None = None,
        connect_retry: bool = True,
    ) -> None:
        # Target omnigraph branch for query/mutate/load; None = the store's main
        # branch. Loads pass `--from main` so the branch forks lazily on first
        # write (docs/BRANCH_INDEXING.md).
        self.branch = branch
        super().__init__(
            graph_uri,
            queries_dir,
            token,
            graph_id=graph_id,
            connect_retry=connect_retry,
        )

    def load(
        self,
        records: list[dict],
        mode: str = "merge",
        *,
        max_bytes: int = chunking.LOAD_MAX_BYTES,
    ) -> None:
        """Bulk-load node/edge records via ``omnigraph load``.

        Each record is a JSONL line: ``{"type": Node, "data": {...}}`` for a
        node or ``{"edge": Edge, "from": key, "to": key}`` for an edge. This
        replaces thousands of per-record ``mutate`` subprocesses — essential for
        indexing large repositories.

        Against a ``--server`` the CLI POSTs the data file as ONE request body,
        so a repo-scale load is split into batches under ``max_bytes`` — see
        :mod:`witan_core.chunking` for the 413 this avoids, and for why every
        node has to be written before any edge. A local store reads the file
        directly and has no such limit, but chunking there costs only a few
        extra Lance versions, which ``omnigraph optimize`` reclaims — not worth
        a second code path.

        ★ ``overwrite`` IS DELIBERATELY NEVER CHUNKED. Measured against 0.8.1,
        that mode TRUNCATES the node type rather than replacing matching rows:
        loading one CodeFile row with ``--mode overwrite`` into a store already
        holding a different one left ``rows=1``, not 2. Split it and every batch
        would erase the one before it, so it stays a single call and an
        oversized one keeps failing loudly at the server.

        ATOMICITY IS TRADED AWAY for the chunked modes, exactly as
        ``change_many(chunk_size=...)`` does: batches commit independently and a
        failure part-way leaves the earlier ones applied. Callers depending on
        all-or-nothing must handle it — ``indexer.index_path`` does, by holding
        each file's ``content_hash`` back until every batch has landed.
        """
        if not records:
            return
        if mode == "overwrite":
            self.load_batch(records, mode)
            return
        for batch in chunking.chunk_records(records, max_bytes):
            self.load_batch(batch, mode)

    # ── Branch operations ─────────────────────────────────────────

    def list_branches(self) -> list[str]:
        """Names of all branches on this store (includes ``main``).

        0.11's ``branch list`` GQ statement on the canonical read route
        (upstream RFC 0055), not an ``omnigraph branch list`` subprocess, so
        branch reads share the transport, retry policy and error
        classification of every other read — see
        :meth:`~witan_core.omnigraph.OmnigraphClient.statement`. Against the
        cluster that is an HTTP POST rather than a process, including on a
        branched client, which the named-query path still cannot use.

        One row per branch, ``{"name": ...}``, under the same ``rows``
        envelope a named read returns — verified identical on both transports
        against the 0.11.0 binary and the 0.11.0 server.
        """
        result = self.statement(
            "query", "branch list", "--format", "json", label="branch list"
        )
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return []
        rows = parsed.get("rows", parsed) if isinstance(parsed, dict) else parsed
        if not isinstance(rows, list):
            return []
        return [
            row["name"]
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        ]

    def ensure_branch(self) -> None:
        """Create ``self.branch`` from main if it doesn't exist yet.

        Needed before the first *read* on a new branch — reads never fork
        (only ``load --from`` does), so a read against a missing branch errors.

        ★ TWO DIFFERENT 409s ARRIVE HERE AND ONLY ONE IS THIS METHOD'S RACE.
        0.11 answers a duplicate create with ``branch 'x' already exists``, and
        it answers a lost write-authority precondition (``write authority
        'graph_head:main' changed during preparation``) with a 409 too.
        ``classify_status`` keys on the status, so it cannot tell them apart,
        and reads both as RETRYABLE.

        Neither default is right on its own. Retrying silently would spend the
        whole budget, backoff sleeps included, re-racing a branch that is
        already there; ``surface_conflict=True`` alone would turn the genuinely
        transient precondition into a hard failure of ``code_store_open``,
        where the old CLI path rode it out. So conflicts are surfaced AND the
        retry is put back here, around a fresh listing — which is what makes
        the duplicate case resolve as "it exists, we are done" instead of as
        another doomed attempt.

        The CLI path produces neither 409: its already-exists is prose matching
        no marker, classifies FATAL, and lands in the ``RuntimeError`` clause.
        """
        if self.branch is None:
            return
        create = f"branch create {_gq_branch_name(self.branch)} from main"
        conflict: RuntimeError | None = None
        for _ in range(_ENSURE_BRANCH_ATTEMPTS):
            if self.branch in self.list_branches():
                return
            try:
                self.statement(
                    "mutate", create, label="branch create", surface_conflict=True
                )
                return
            except OmnigraphConflict as exc:
                conflict = exc
            except RuntimeError as exc:
                # The CLI's own already-exists. Two concurrent
                # `code_store_open` calls for one view both saw it missing and
                # both tried to create it. Anything else is a real failure.
                if "already exists" not in str(exc):
                    raise
                conflict = exc
        # The message alone is never trusted: the branch has to actually be
        # there, which is all this method promises.
        if self.branch not in self.list_branches():
            raise conflict  # noqa: RSE102 — the loop cannot exit without one

    def delete_branch(self, name: str) -> None:
        # `--yes` is for the CLI path only, where a destructive write against a
        # non-local scope refuses without it; the HTTP route has no prompt to
        # skip and ignores flags entirely.
        self.statement(
            "mutate",
            f"branch delete {_gq_branch_name(name)}",
            "--yes",
            label="branch delete",
        )

    def branch_last_write(self, name: str) -> float | None:
        """When ``name`` was last written, as epoch seconds, or ``None``.

        ``None`` means the branch has no commits *of its own* — every commit
        reachable from it came from the branch it forked off. That is a view
        created but never indexed, and it is not the same as "old": there is no
        branch-creation timestamp anywhere in omnigraph 0.8.1 to age it by. The
        reaper treats it accordingly (:mod:`witan_code.reaper`).

        ``omnigraph branch list`` returns bare names, so staleness has to come
        from the commit log: ``commit list --branch`` returns every reachable
        commit, each tagged with the ``manifest_branch`` it landed on, so the
        branch's own writes are the ones tagged with it.

        Output that isn't the expected JSON shape **raises**, and must not
        degrade to ``None``: ``None`` is load-bearing here — it tells the
        reaper never to touch this view — so a format change or a stray
        warning line on stdout would quietly turn a scheduled reaper into a
        no-op that reports success while branch sprawl grows unbounded. Same
        convention as the base client's :meth:`read`.
        """
        result = self._run("commit", "list", "--branch", name, "--json")
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"omnigraph commit list returned non-JSON for branch "
                f"{name!r}: {result!r}"
            ) from exc
        commits = parsed.get("commits") if isinstance(parsed, dict) else parsed
        if not isinstance(commits, list):
            raise RuntimeError(
                f"omnigraph commit list returned no commit array for branch "
                f"{name!r}: {parsed!r}"
            )
        # ★ TWO FIELD NAMES, ONE MEANING. omnigraph's 2026-08-20 vocabulary
        # sweep (upstream ecf1d6aedd, #538) renamed `manifest_branch` to
        # `graph_branch` in `commit list --json` without bumping the reported
        # version, so both are in the wild. Reading only one is not a cosmetic
        # miss here: no row matches, `stamps` is empty, and this returns None —
        # which the docstring above makes load-bearing as "never reap this
        # view". The reaper would then quietly stop reaping anything at all,
        # and nothing would raise to say so.
        stamps = [
            row["created_at"]
            for row in commits
            if isinstance(row, dict)
            and name in (row.get("manifest_branch"), row.get("graph_branch"))
            and isinstance(row.get("created_at"), (int, float))
        ]
        # created_at is microseconds since the epoch.
        return max(stamps) / 1_000_000 if stamps else None

    def _extra_args(self, subcommand: str) -> list[str]:
        # optimize/cleanup compact the whole store (every branch), not a single
        # one, so they never take --branch even on a branched client. `commit`
        # names its branch as its own flag, so injecting this client's would
        # either duplicate it or silently retarget the call.
        #
        # `branch` is NOT in this list any more because nothing runs it: branch
        # work goes through `statement`, which bypasses `_extra_args` entirely
        # (a GQ branch statement takes no --branch). Re-adding the subcommand
        # here would be dead code, not a safety net.
        if self.branch is None or subcommand in ("commit", "optimize", "cleanup"):
            return []
        if subcommand == "load":
            return ["--branch", self.branch, "--from", "main"]
        return ["--branch", self.branch]
