"""Stage 2 — cross-repo symbol stitching at read time (docs/SYMBOL_TABLE.md).

Joins ``RepoSymbol`` rows across the whole bridge store to compute precise
cross-repo edges WITHOUT ever writing them: each repo's ``external``
(unresolved reference) rows are matched against every OTHER repo's
``exported`` rows by canonical symbol identity. This is the read-time join
Stage 1 exists to serve — the RANGER/SCIP pattern of deferring cross-repo
linking to query time instead of guessing at extraction time.

Join keys (docs/SYMBOL_TABLE.md § Stage-2 join contract):
  * ``env`` / ``svc`` — exact ``(scheme, descriptor)``.
  * ``http`` / ``pkg`` — ``(scheme, key_norm)``; ``http`` additionally
    requires method compatibility (a consumer method of ``*`` matches any
    provider method) since key_norm collapses the method-bearing descriptor
    down to the bare path. ``pkg`` descriptors are always ``.``
    (SYMBOL_FORMAT.md), so key_norm — which carries the package name for
    both roles — is the only usable join key for packages.

Version disambiguation follows SYMBOL_FORMAT.md decision 1: prefer an exact
version match, then ``main``, else flag the group ``ambiguous_version``
rather than guessing a winner. Every candidate is still returned as its own
edge (this project's consistent pattern of surfacing all cross-repo data and
letting callers filter — see ``code_cross_repo_impact``); ``preferred`` marks
which candidate(s) survive the version-preference rule and ``match_count``
lets a caller tell a clean single match from a fan-out before deciding
whether to trust it.

Existing URL-heuristic extraction (``visualize.cross_repo_edges``, keyed on
the coarser ``(kind, key_norm)`` binding grouping) is the Stage-3 fallback
tier for external symbols that fail to join here — see
``get_unresolved_symbols`` below.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PreciseEdge:
    consumer_repo: str
    consumer_symbol: str
    provider_repo: str
    provider_symbol: str
    kind: str
    scheme: str
    match_count: int
    preferred: bool
    ambiguous_version: bool

    def as_dict(self) -> dict:
        return {
            "consumer_repo": self.consumer_repo,
            "consumer_symbol": self.consumer_symbol,
            "provider_repo": self.provider_repo,
            "provider_symbol": self.provider_symbol,
            "kind": self.kind,
            "scheme": self.scheme,
            "match_count": self.match_count,
            "preferred": self.preferred,
            "ambiguous_version": self.ambiguous_version,
        }


def _method(descriptor: str) -> str | None:
    """The leading method token of an http descriptor, or None if there isn't one."""
    method, _, rest = descriptor.partition(" ")
    return method if rest else None


def _http_compatible(consumer_descriptor: str, provider_descriptor: str) -> bool:
    consumer_method = _method(consumer_descriptor)
    return consumer_method in (None, "*") or consumer_method == _method(
        provider_descriptor
    )


def _join_key(row: dict) -> tuple:
    scheme = row["scheme"]
    if scheme in ("env", "svc"):
        return (scheme, "descriptor", row["descriptor"])
    return (scheme, "key_norm", row["key_norm"])


def _select_preferred(
    consumer_version: str, candidates: list[dict]
) -> tuple[set, bool]:
    """Indices of the preferred candidate(s) plus whether the group is ambiguous.

    SYMBOL_FORMAT.md decision 1: prefer an exact version match, then ``main``,
    else every remaining candidate is preferred and the group is ambiguous
    (more than one — don't guess a winner).
    """

    def where(pred) -> set:
        return {i for i, c in enumerate(candidates) if pred(c)}

    if consumer_version not in (".", ""):
        exact = where(lambda c: c["version"] == consumer_version)
        if exact:
            return exact, len(exact) > 1
    main = where(lambda c: c["version"] == "main")
    if main:
        return main, len(main) > 1
    return set(range(len(candidates))), len(candidates) > 1


def resolve(rows: list[dict]) -> tuple[list[PreciseEdge], list[dict]]:
    """Join a full ``RepoSymbol`` dump (``all_repo_symbols``) into edges + gaps.

    Returns ``(precise_edges, unresolved)``: every external row that matched
    at least one exported row from another repo becomes one ``PreciseEdge``
    per candidate; external rows with zero candidates are returned verbatim
    in ``unresolved`` (the raw ``RepoSymbol`` dict) for
    ``get_unresolved_symbols`` / Stage-3 fallback.
    """
    exported: dict[tuple, list[dict]] = {}
    external: list[dict] = []
    for row in rows:
        if row["role"] == "exported":
            exported.setdefault(_join_key(row), []).append(row)
        else:
            external.append(row)

    edges: list[PreciseEdge] = []
    unresolved: list[dict] = []
    for ext in external:
        candidates = [
            prov
            for prov in exported.get(_join_key(ext), [])
            if prov["repo"] != ext["repo"]
            and (
                ext["scheme"] != "http"
                or _http_compatible(ext["descriptor"], prov["descriptor"])
            )
        ]
        if not candidates:
            unresolved.append(ext)
            continue
        preferred_idx, ambiguous = _select_preferred(ext["version"], candidates)
        for i, prov in enumerate(candidates):
            edges.append(
                PreciseEdge(
                    consumer_repo=ext["repo"],
                    consumer_symbol=ext["symbol"],
                    provider_repo=prov["repo"],
                    provider_symbol=prov["symbol"],
                    kind=ext["kind"],
                    scheme=ext["scheme"],
                    match_count=len(candidates),
                    preferred=i in preferred_idx,
                    ambiguous_version=ambiguous,
                )
            )
    return edges, unresolved
