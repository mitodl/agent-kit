"""Typed cross-repo edge precision tiers (docs/EDGE_PRECISION_TIERS.md).

Kythe-inspired ``ref/call/direct`` vs ``ref/call`` pattern: replace the
previous binary consumer/provider model with typed edges that carry the
precision of *how* a cross-repo link was established, so callers filter by a
minimum trust floor instead of a confidence threshold they have to pick
themselves.

Tiers, in decreasing trust order:
  * ``precise``   — Stage 2: canonical symbol string join (``stitch``).
  * ``heuristic`` — Stage 3: the pre-existing ``(kind, key_norm)`` binding
    grouping, confidence-scored (``bridge_extractors.adjust_confidence``,
    ``visualize.cross_repo_edges``).
  * ``fuzzy``     — future: embedding/BM25 route similarity (tracked
    separately: "Research: REST-route cross-service matching via embedding
    similarity"). No fuzzy edges exist yet, so this tier is currently
    identical to ``heuristic``.

``min_precision`` is a FLOOR: ``"precise"`` returns only the precise tier;
``"heuristic"`` (the default, preserving pre-existing behavior) returns
precise + heuristic; ``"fuzzy"`` returns everything. A heuristic edge is
suppressed whenever a precise edge already covers the same
``(consumer_repo, provider_repo, kind, key_norm)`` triple, so the same
logical link never appears twice at two precision tiers.
"""

from dataclasses import dataclass

from . import stitch
from .visualize import cross_repo_edges as _confidence_filter

PRECISION_TIERS = ("precise", "heuristic", "fuzzy")
_TIER_ORDER = {tier: i for i, tier in enumerate(PRECISION_TIERS)}


@dataclass(frozen=True)
class TypedEdge:
    precision: str  # "precise" | "heuristic" | "fuzzy"
    consumer_repo: str
    provider_repo: str
    kind: str
    key_norm: str
    canonical_symbol: str | None  # the consumer's symbol; precise tier only
    confidence: float
    evidence: tuple[dict, ...]

    def as_dict(self) -> dict:
        return {
            "precision": self.precision,
            "consumer_repo": self.consumer_repo,
            "provider_repo": self.provider_repo,
            "kind": self.kind,
            "key_norm": self.key_norm,
            "canonical_symbol": self.canonical_symbol,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }


def _pair(edge) -> tuple:
    return (edge.consumer_repo, edge.provider_repo, edge.kind, edge.key_norm)


def _precise_edges(repo_symbol_rows: list[dict]) -> list[TypedEdge]:
    precise, _ = stitch.resolve(repo_symbol_rows)
    return [
        TypedEdge(
            precision="precise",
            consumer_repo=e.consumer_repo,
            provider_repo=e.provider_repo,
            kind=e.kind,
            key_norm=e.key_norm,
            canonical_symbol=e.consumer_symbol,
            confidence=1.0,
            evidence=e.evidence,
        )
        for e in precise
    ]


def _heuristic_edges(
    binding_rows: list[dict], *, min_confidence: float
) -> list[TypedEdge]:
    """Group raw ``InterfaceBinding`` rows into (consumer, provider) edges.

    Mirrors ``visualize.build_graph``'s grouping/self-providing/service-anchor
    rules but keeps per-occurrence evidence instead of collapsing into a
    render-oriented ``DepGraph``.
    """
    filtered = _confidence_filter(binding_rows, min_confidence=min_confidence)

    groups: dict[tuple[str, str], dict] = {}
    for b in filtered:
        if b["kind"] == "service":
            continue  # service anchors aren't repo-to-repo edges (see visualize.build_graph)
        group = groups.setdefault((b["kind"], b["key_norm"]), {"p": {}, "c": {}})
        bucket = group["p"] if b["role"] == "provider" else group["c"]
        repo_bucket = bucket.setdefault(b["repo"], {"confidence": None, "evidence": []})
        if b["role"] != "provider":
            conf = float(b["confidence"]) if b.get("confidence") is not None else 1.0
            if repo_bucket["confidence"] is None or conf > repo_bucket["confidence"]:
                repo_bucket["confidence"] = conf
        if b.get("file"):
            repo_bucket["evidence"].append(
                {"repo": b["repo"], "file": b["file"], "line": b.get("line")}
            )

    out: list[TypedEdge] = []
    for (kind, key_norm), group in groups.items():
        for cons_repo, cons in group["c"].items():
            if cons_repo in group["p"]:
                continue  # self-providing: the repo serves its own route
            for prov_repo in group["p"]:
                if cons_repo == prov_repo:
                    continue
                out.append(
                    TypedEdge(
                        precision="heuristic",
                        consumer_repo=cons_repo,
                        provider_repo=prov_repo,
                        kind=kind,
                        key_norm=key_norm,
                        canonical_symbol=None,
                        confidence=(
                            cons["confidence"]
                            if cons["confidence"] is not None
                            else 1.0
                        ),
                        evidence=tuple(cons["evidence"]),
                    )
                )
    return out


def cross_repo_edges(
    repo_symbol_rows: list[dict],
    binding_rows: list[dict],
    *,
    min_precision: str = "heuristic",
    min_confidence: float = 0.5,
) -> list[TypedEdge]:
    """Merge Stage 2 precise + Stage 3 heuristic edges into one typed, filterable list.

    ``repo_symbol_rows`` is a full ``RepoSymbol`` dump (``all_repo_symbols``);
    ``binding_rows`` is a full ``InterfaceBinding`` dump (``all_bindings``).
    See the module docstring for tier semantics.
    """
    if min_precision not in _TIER_ORDER:
        raise ValueError(f"min_precision must be one of {PRECISION_TIERS!r}")

    precise = _precise_edges(repo_symbol_rows)
    out = list(precise)
    if _TIER_ORDER[min_precision] >= _TIER_ORDER["heuristic"]:
        covered = {_pair(e) for e in precise}
        heuristic = _heuristic_edges(binding_rows, min_confidence=min_confidence)
        out.extend(e for e in heuristic if _pair(e) not in covered)
    return out


def precise_pairs(repo_symbol_rows: list[dict]) -> frozenset[tuple[str, str, str, str]]:
    """``(consumer_repo, provider_repo, kind, key_norm)`` covered by a Stage-2 join.

    A cheap membership-test helper for tools that need to know whether a
    *specific* binding participates in a precise edge without building the
    full merged/typed edge list — e.g. filtering ``code_interface_*`` rows by
    ``min_precision="precise"``.
    """
    precise, _ = stitch.resolve(repo_symbol_rows)
    return frozenset(
        (e.consumer_repo, e.provider_repo, e.kind, e.key_norm) for e in precise
    )
