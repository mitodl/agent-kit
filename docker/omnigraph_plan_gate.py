#!/usr/bin/env python3
"""Pre-deploy gate for the omnigraph data tier (omnigraph 0.13 to 0.15).

The server entrypoint applies the baked configuration on every boot, after
`Recreate` has already stopped the old pod. A schema or policy change the
apply refuses would therefore be found by a pod that cannot start, with
nothing serving. This runs the entrypoint's own plan check with the new image
against the served root, as a Job the Deployment depends on, so the same
refusal fails the deploy while the old pod is still up.

`cluster plan` reads the root without taking the writer lock (on 0.14 too:
"Plans are observations and never reserve writer authority", omnigraph v0.14.0
`crates/omnigraph-cluster/src/types.rs:231`), so this is not a second writer
beside the running server and needs neither the unlock Lease nor the pod
check. It never unlocks and never applies. The entrypoint's `Cluster` picks
the 0.13 or 0.14 argument shape.

A schema change refused only because graphs in
OMNIGRAPH_BRANCH_DROP_ALLOWLIST have branch views is cleared here: the views
are deleted through the serving pod at OMNIGRAPH_VIEW_DROP_SERVER_URL, as
OMNIGRAPH_VIEW_DROP_ACTOR, and the plan is made again. Those are ordinary
served writes, so the old pod keeps serving through a drop of any size and
the boot that follows only finds views written since. A pod that does not
answer (it is down) leaves every view to the boot. The same refusal on a
graph that is not allow-listed still fails the gate (see the entrypoint's
``views_to_drop``).

The entrypoint still runs the same check on every boot. The gate only moves
the first failure earlier: the `cluster.yaml` ConfigMap is updated before this
Job runs and a failed Job does not roll it back.

Design: docs/internals/design/omnigraph-0-13-upgrade-spec.md (D4) and
omnigraph-0-14-upgrade-spec.md (D3).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from omnigraph_server_entrypoint import (
    DEFAULT_CLUSTER_DIR,
    BootRefused,
    Cluster,
    NoPeers,
    ServedGraphs,
    ServerUnavailable,
    Termination,
    blocked_graphs,
    branch_drop_allowlist,
    checked_plan,
    cluster_layout,
    drop_branch_views,
    log,
    view_drop_token,
    views_to_drop,
)


def drop_served_views(
    cluster: Cluster, config_dir: Path, env: dict[str, str], blocked: list[str]
) -> list[str]:
    """Delete ``blocked`` graphs' views through the serving pod, then re-plan.

    :returns: the graphs still blocked by views, written since they were
        listed or left because the pod did not answer. The boot drops those.
    :raises BootRefused: on a plan refusal the drop uncovered, or a blocked
        graph that is not allow-listed and has branches.
    """
    served = ServedGraphs(env["OMNIGRAPH_VIEW_DROP_SERVER_URL"], view_drop_token(env))
    droppable = branch_drop_allowlist(env, cluster_layout(config_dir)[1])
    lease = NoPeers(may_unlock=False)
    try:
        views = views_to_drop(served, blocked, droppable)
        # A Job pod's SIGTERM is not watched: a stopped gate fails the deploy.
        drop_branch_views(served, views, Termination(0), lease)
        blocked = blocked_graphs(cluster, config_dir, env)
        if not blocked:
            return []
        views = views_to_drop(served, blocked, droppable)
    except ServerUnavailable as unavailable:
        log(f"cannot drop branch views through {served.base_url}: {unavailable}")
        return blocked
    for graph, branches in views.items():
        log(f"{graph} has {len(branches)} branch views written during the drop")
    return blocked


def main(env: dict[str, str]) -> int:
    config_dir = Path(env.get("OMNIGRAPH_CLUSTER_DIR", DEFAULT_CLUSTER_DIR))
    try:
        cluster, blocked = checked_plan(config_dir, env, NoPeers(may_unlock=False))
        if blocked:
            blocked = drop_served_views(cluster, config_dir, env, blocked)
    except BootRefused as refusal:
        print(f"omnigraph-server: plan refused: {refusal}", file=sys.stderr)
        return 1
    if blocked:
        log(
            f"{', '.join(blocked)} still have branch views blocking their "
            "schema change; the next boot drops them"
        )
    log(f"the plan for {config_dir} can be applied")
    return 0


if __name__ == "__main__":
    sys.exit(main(dict(os.environ)))
