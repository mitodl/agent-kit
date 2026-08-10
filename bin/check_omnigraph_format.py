#!/usr/bin/env -S uv run --quiet --package witan-core python
"""Fail when the pinned omnigraph binary reads a different storage format than
this repo declares.

WHY. omnigraph uses strict single-version storage: a binary refuses a graph
written under a different on-disk format, in both directions, and there is no
downgrade. A release that moves that format therefore invalidates every local
store and every deployed graph at once — 0.8.1 → 0.9.0 moved it from 4 to 6.

Renovate bumps ``_OMNIGRAPH_VERSION`` and knows nothing about any of that. It
has automerge enabled for this dependency. What stopped 0.9.0 from merging
itself was a minimum-release-age timer and an unrelated red job — neither of
which is a control.

So the repo DECLARES the format it expects (``_OMNIGRAPH_INTERNAL_SCHEMA``) and
this check asserts the binary agrees. A version bump that moves the format
leaves the two disagreeing and fails here, with the consequences spelled out.
Making it pass means editing the declaration, which is a human saying "yes,
I know this rebuilds every graph".

Deliberately compares binary-against-declaration rather than old-pin-against-
new-pin: it needs one binary instead of two, it holds on ``main`` and not only
on pull requests, and it still fires if the format moves without the version
changing (a re-cut release, a rebuilt image).
"""

import shutil
import sys
from pathlib import Path

import cyclopts
from witan_core.omnigraph_install import (
    _OMNIGRAPH_INTERNAL_SCHEMA,
    _OMNIGRAPH_VERSION,
    _installed_version,
    reported_internal_schema,
)

app = cyclopts.App(
    name="check-omnigraph-format",
    help="Assert the pinned omnigraph binary reads the declared storage format.",
)

_DECLARATION = (
    "packages/witan-core/witan_core/omnigraph_install.py :: _OMNIGRAPH_INTERNAL_SCHEMA"
)


@app.default
def check(binary: str = "omnigraph") -> None:
    """Compare ``binary``'s reported internal-schema to the declared one.

    Parameters
    ----------
    binary
        Path to the omnigraph binary to interrogate. Defaults to whatever is
        on PATH, which in CI is the pinned release the install step fetched.
    """
    try:
        actual = reported_internal_schema(binary)
    except RuntimeError as exc:
        print(f"could not read the binary's storage format:\n{exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # The binary's OWN version, not the pin. They match in CI, where this runs
    # against what the install step fetched — but reporting the declared pin
    # for a binary passed via --binary would name the wrong release in the one
    # message a reader is going to act on.
    resolved = shutil.which(binary) or binary
    running = _installed_version(Path(resolved)) or "unknown version"

    if actual == _OMNIGRAPH_INTERNAL_SCHEMA:
        print(f"omnigraph {running} reads storage format {actual}, as declared.")
        return

    direction = "newer" if actual > _OMNIGRAPH_INTERNAL_SCHEMA else "older"
    pin_note = (
        ""
        if running == _OMNIGRAPH_VERSION
        else f"\n(Checked binary {running}; the repo pin is {_OMNIGRAPH_VERSION}.)"
    )
    print(
        f"""
STORAGE FORMAT BREAK

  omnigraph {running} reads on-disk format {actual}
  this repo declares                      {_OMNIGRAPH_INTERNAL_SCHEMA}  ({direction}){pin_note}

This is not a lint failure. Merging this bump as-is means:

  * Every developer's local graph stops opening, at once, on their next
    `witan setup`. Recovery is `witan migrate storage`, which exports with the
    PREVIOUS binary — kept as `omnigraph-<version>` beside the new one.

  * Every deployed graph must be rebuilt: export with the old binary, `init` a
    DIFFERENT root with the new one, `load --mode overwrite`, repoint
    cluster.yaml, `cluster apply`.

  * There is no downgrade and no canary. A {_OMNIGRAPH_INTERNAL_SCHEMA}-format binary refuses a
    {actual}-format graph and vice versa, so the fleet cannot run mixed and
    rollback means repointing at the retained old root.

Do NOT edit the declaration to clear this check. Edit it as the LAST step of a
planned migration:

  1. Verify the wire contracts against the new binary — the export and query
     shapes are not covered by the release notes. See
     packages/witan-core/tests/test_binary_contract.py :: _CONTRACTS
  2. Plan the deployed rebuild, per environment.
  3. Then set {_DECLARATION} = {actual}
""".strip(),
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    app()
