#!/usr/bin/env python3
"""Render deployed Cedar group membership from the live actor-token map.

The committed bundles carry ILLUSTRATIVE membership (`act-alice`, `act-bob`,
`act-svc-witan-ci`). Those ids are fixtures for `policy test`; none of them
exists in a deployed cluster. This script replaces every bundle's `groups:`
with the actors that actually hold a bearer token, and is run by
`docker/omnigraph-server-entrypoint.sh` immediately before `omnigraph cluster
apply` — see policy/README.md § "Deploying (ol-infrastructure)".

WHY AT BOOT, RATHER THAN AT DEPLOY TIME

`witan-users` has to track the actor set that the hourly `witan-token-sync`
CronJob writes to `secret-operations/witan/actor-tokens`. Pulumi cannot see
that set — the job owns the Vault path, not the stack — so a Pulumi-rendered
list is stale by construction: a user provisioned at 10:00 would authenticate
and then be denied everything until someone ran a deploy, which reads as a
broken client rather than a missing grant.

Rendering here closes that gap without adding a moving part. The `actor-tokens`
VaultStaticSecret declares `rolloutRestartTargets: [omnigraph-server]`, so the
server ALREADY restarts on exactly the event that changes the actor set. Doing
the render on the restart path means the token map and the policy can never
disagree: whatever mounted tokens.json says at boot is what the bundles grant.

MAPPING

The token map is keyed by actor id. Humans are `act-<slug>`
(`witan_core.identity.derive_actor_id`); service accounts are named literally.
Note the service ids in the map are NOT `act-` prefixed, which is exactly where
a naive copy of the fixture bundles would deny the CI and break-glass identities
everything.

    act-*             -> witan-users
    svc-witan-ci      -> witan-ci
    svc-witan         -> witan-service
    svc-witan-admin   -> witan-admin

A group named in a bundle but with no provisioned member renders as an empty
list. That is intentional and load-bearing: `svc-witan` is not provisioned in
any environment yet (its rules simply grant nobody), and an empty group is far
safer than inventing an id that silently matches no token. Every rendered group
is logged with its size so an unexpectedly empty one is visible in the pod log
rather than surfacing later as a denial nobody can explain.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

# Actor-id prefix for authenticated humans, mirroring
# witan_core.identity.derive_actor_id and the ACTOR_PREFIX in
# ol-infrastructure's scripts/sync_actor_tokens.py.
HUMAN_ACTOR_PREFIX = "act-"

# Service actor id -> the Cedar group it belongs to. Keys are the literal keys
# of the actor-token map, which ol-infrastructure's omnigraph stack provisions
# from SOPS; they carry no `act-` prefix.
SERVICE_ACTOR_GROUPS = {
    "svc-witan-ci": "witan-ci",
    "svc-witan": "witan-service",
    "svc-witan-admin": "witan-admin",
}

# Every group name the bundles may declare. A bundle naming anything outside
# this set means the bundle and this renderer have drifted apart, which would
# otherwise render that group empty and deny its members silently.
KNOWN_GROUPS = {"witan-users", *SERVICE_ACTOR_GROUPS.values()}


class RenderError(Exception):
    """A condition that must stop the boot rather than deny everyone at runtime."""


def classify_actors(actor_ids: list[str]) -> dict[str, list[str]]:
    """Bucket provisioned actor ids into Cedar groups.

    Unknown non-human ids are an error, not a silent drop: an actor holding a
    valid token but belonging to no group can authenticate and do nothing, and
    the resulting denial names an actor that looks correctly provisioned.
    """
    groups: dict[str, list[str]] = {name: [] for name in KNOWN_GROUPS}
    unknown: list[str] = []
    for actor_id in actor_ids:
        if actor_id.startswith(HUMAN_ACTOR_PREFIX):
            groups["witan-users"].append(actor_id)
        elif actor_id in SERVICE_ACTOR_GROUPS:
            groups[SERVICE_ACTOR_GROUPS[actor_id]].append(actor_id)
        else:
            unknown.append(actor_id)
    if unknown:
        msg = (
            "actor-token map holds ids belonging to no Cedar group: "
            f"{sorted(unknown)}. Add them to SERVICE_ACTOR_GROUPS (or give them "
            f"the '{HUMAN_ACTOR_PREFIX}' prefix if they are humans) — as-is they "
            "would authenticate and then be denied every action."
        )
        raise RenderError(msg)
    for members in groups.values():
        members.sort()
    return groups


def load_actor_ids(tokens_path: Path) -> list[str]:
    """Read the actor ids out of the mounted `{actor_id: token}` map."""
    try:
        raw = json.loads(tokens_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"cannot read actor-token map at {tokens_path}: {exc}"
        raise RenderError(msg) from exc
    if not isinstance(raw, dict) or not raw:
        msg = (
            f"actor-token map at {tokens_path} is not a non-empty object; "
            "applying a bundle against it would deny every request"
        )
        raise RenderError(msg)
    return list(raw)


def render_bundle(path: Path, groups: dict[str, list[str]]) -> list[str]:
    """Rewrite one bundle's `groups:` in place. Returns the group names written.

    Only groups the bundle already declares are written — the bundles are
    deliberately scoped (the memory graph declares no `witan-ci`, since the
    code-graph pipeline has no role in the work graph), and adding a group a
    bundle never references would be noise at best.
    """
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict) or "groups" not in doc:
        msg = f"{path.name}: not a policy bundle (no top-level `groups`)"
        raise RenderError(msg)
    declared = list(doc["groups"])
    unknown = sorted(set(declared) - KNOWN_GROUPS)
    if unknown:
        msg = (
            f"{path.name}: declares group(s) {unknown} that this renderer does "
            "not know how to populate; they would render empty and deny their "
            "members. Bundle and renderer have drifted."
        )
        raise RenderError(msg)
    doc["groups"] = {name: groups[name] for name in declared}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return declared


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Render Cedar group membership from the actor-token map."
    )
    parser.add_argument(
        "--tokens",
        required=True,
        type=Path,
        help="Path to the mounted {actor_id: token} JSON map.",
    )
    parser.add_argument(
        "bundles",
        nargs="+",
        type=Path,
        help="Bundle files to rewrite in place.",
    )
    args = parser.parse_args(argv)

    try:
        actor_ids = load_actor_ids(args.tokens)
        groups = classify_actors(actor_ids)
        for path in args.bundles:
            written = render_bundle(path, groups)
            sizes = ", ".join(f"{name}={len(groups[name])}" for name in written)
            print(f"render-policy-groups: {path.name} [{sizes}]")
    except RenderError as exc:
        print(f"render-policy-groups: {exc}", file=sys.stderr)
        return 1

    empty = sorted(name for name, members in groups.items() if not members)
    if empty:
        # Not fatal: `svc-witan` (witan-service) is deliberately unprovisioned
        # today. Surfaced loudly because an unexpectedly empty group is
        # otherwise indistinguishable from a working deployment until someone
        # hits a denial.
        print(
            f"render-policy-groups: WARNING empty group(s): {empty} — "
            "rules referencing them grant nobody",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
