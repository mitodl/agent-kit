#!/usr/bin/env python3
"""Pre-deploy gate for the omnigraph data tier (omnigraph 0.13 and 0.14).

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
    NoPeers,
    checked_plan,
    log,
)


def main(env: dict[str, str]) -> int:
    config_dir = Path(env.get("OMNIGRAPH_CLUSTER_DIR", DEFAULT_CLUSTER_DIR))
    try:
        checked_plan(config_dir, env, NoPeers(may_unlock=False))
    except BootRefused as refusal:
        print(f"omnigraph-server: plan refused: {refusal}", file=sys.stderr)
        return 1
    log(f"the plan for {config_dir} can be applied")
    return 0


if __name__ == "__main__":
    sys.exit(main(dict(os.environ)))
