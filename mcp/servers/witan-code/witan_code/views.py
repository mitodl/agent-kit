"""Branch-view naming: who owns an omnigraph branch, and what it is a view of.

A code-graph branch view must have exactly ONE writer and be readable by
everyone. On a local store both hold for free — one machine, one user. On a
shared cluster graph neither does: ``branch_store_name(current_branch())``
derives the same name from the git branch alone, so two checkouts on
``feature-x`` (two developers, or one developer in two worktrees, or an agent
and its human) write the same view and overwrite each other with different
working-tree states. The symptom is a symbol resolving to somebody else's
uncommitted version of it.

So the writer goes in the name. One scheme covers both stores::

    per-repo graph:  [<actor>/]<branch>
    bridge graph:    [<actor>/]<repo-slug>/<branch>

The actor comes first in both, which is the point: ownership is a *prefix*
predicate. "Does this actor own this view" is one string comparison here, the
stale-view reaper can sweep by owner, and a Cedar rule can gate writes with
``startsWith(branch, principal.actor + "/")`` without knowing which of the two
stores it is looking at (tk-branch-cedar-gating-stale-code-graph-branch-reap).

The ``<actor>/`` prefix is absent exactly when this process has no identity to
name — no deployment configured, so nothing shared to collide on
(:mod:`witan_code.identity`). Purely local use therefore keeps the view names
it already has: no existing store needs migrating, and ``branches --prune``
keeps comparing like with like. There is no second rule for local stores; a
logged-in user's local views simply carry their owner too, which costs
nothing and keeps one naming scheme rather than two that can disagree.

Every component is separator-free by construction — an actor id is
``act-[a-z0-9-]+`` (:func:`witan_core.identity.derive_actor_id`), a repo slug
has ``[/:]+`` collapsed to ``_`` (:func:`config.sanitize_slug`), and a branch
component has everything outside ``[A-Za-z0-9._-]`` collapsed to ``_``
(:func:`repo.sanitize_branch`) — so a name splits back into its parts
unambiguously.

Sanitizing the branch is one-way (``feature/new-api`` → ``feature_new-api``),
so a view name does NOT round-trip to a git branch name. The mapping is only
ever used in the direction that has an authority for the raw name: witan's
``CodeBranch`` (``<repo URI>|<git branch>``) holds it, and a consumer
sanitizes at the edge to reach the view — see docs/BRANCH_INDEXING.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from witan_core.identity import ACTOR_PREFIX

from . import config as cfg_module

__all__ = [
    "SEPARATOR",
    "BranchView",
    "bridge_view",
    "owner",
    "parse_view",
    "repo_view",
    "views_for_branch",
]

SEPARATOR = "/"
"""Component separator. Not produced by any component's own sanitizer."""


@dataclass(frozen=True)
class BranchView:
    """One omnigraph branch, decomposed into the parts of its name.

    ``branch`` is the sanitized git-branch component (never a raw git branch —
    see the module docstring). ``actor`` is the view's single writer, or
    ``None`` on an un-namespaced local store. ``repo`` is set only for views on
    the shared bridge graph, where one graph carries every repo's bindings.
    """

    branch: str
    actor: str | None = None
    repo: str | None = None

    @property
    def name(self) -> str:
        """The omnigraph branch name — what goes on the wire as ``--branch``."""
        parts = [p for p in (self.actor, self.repo, self.branch) if p]
        return SEPARATOR.join(parts)


def repo_view(branch: str, *, actor: str | None) -> str:
    """Name of ``actor``'s view of ``branch`` on a per-repo code graph."""
    return BranchView(branch=branch, actor=actor).name


def bridge_view(branch: str, repo: str, *, actor: str | None) -> str:
    """Name of ``actor``'s view of ``repo``'s ``branch`` on the bridge graph."""
    return BranchView(
        branch=branch, actor=actor, repo=cfg_module.sanitize_slug(repo)
    ).name


def owner(name: str) -> str | None:
    """The actor that owns view ``name``, or ``None`` if it is un-namespaced.

    Store-kind agnostic — both schemes put the actor first — so the write
    guard can answer "do I own what I am about to write" without being told
    which graph it is writing to.

    A leading component only counts as an owner when something follows it: a
    git branch literally named ``act-foo`` sanitizes to the single-component
    view ``act-foo``, which is a branch, not an owner.
    """
    head, sep, rest = name.partition(SEPARATOR)
    if sep and rest and head.startswith(ACTOR_PREFIX):
        return head
    return None


def parse_view(name: str, *, bridge: bool = False) -> BranchView:
    """Decompose a view name. ``bridge`` selects the repo-qualified scheme.

    Unrecognized shapes (too many components, or a repo qualifier where none
    belongs) degrade to a single opaque ``branch`` rather than raising: this
    runs over whatever ``branch list`` returns, which can include branches no
    version of witan-code created.
    """
    actor = owner(name)
    rest = name[len(actor) + 1 :] if actor else name
    if not bridge:
        return BranchView(branch=rest, actor=actor)
    repo, sep, branch = rest.partition(SEPARATOR)
    if not sep or SEPARATOR in branch:
        return BranchView(branch=rest, actor=actor)
    return BranchView(branch=branch, actor=actor, repo=repo)


def views_for_branch(
    names: list[str],
    branch: str,
    *,
    bridge: bool = False,
    repo: str | None = None,
) -> list[BranchView]:
    """Every view of ``branch`` in ``names``, whoever owns it.

    ``branch`` is the sanitized branch *component* — what
    ``repo_module.store_branch()`` returns, or what
    ``repo_module.branch_store_name()`` maps a raw git branch to. Mapping
    happens once, at the edge that first sees a raw name; ``branch_store_name``
    is not idempotent (``_detached`` sanitizes to ``detached``), so applying it
    again here would quietly stop matching the scratch view.

    This is the read side of per-writer namespacing, and the reason the
    decision to keep branch views on the shared graph was taken: an agent can
    enumerate every in-flight view of the branch it is working on, including
    the ones its teammates are still writing.
    """
    slug = cfg_module.sanitize_slug(repo) if repo else None
    parsed = (parse_view(n, bridge=bridge) for n in names)
    return sorted(
        (v for v in parsed if v.branch == branch and (slug is None or v.repo == slug)),
        key=lambda v: (v.actor or "", v.repo or ""),
    )
