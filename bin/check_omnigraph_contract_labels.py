#!/usr/bin/env -S uv run --quiet --package witan-core python
"""Fail when an image's HTTP-contract label drifts from witan-core's declaration.

ol-infrastructure deploys witan and omnigraph-server from separate Concourse
gates with no ordering between them. On 2026-11-04 the Production 0.14 cutover
released both together, the server finished first, and the running witan
(contract 0.13 only) failed every graph call for about 15 minutes. Contract
negotiation removes that window only when the negotiating client is running
before the server changes contract.

So each image says which contracts it speaks, and the omnigraph stack's preview
refuses a server image whose contract the deployed witan image can't speak:

* ``docker/omnigraph-server.Dockerfile`` stamps ``OMNIGRAPH_HTTP_API_CONTRACT``
  as ``edu.mit.ol.omnigraph.http-api-contract``. It must equal
  ``HTTP_API_CONTRACT``, which ``bin/check_omnigraph_format.py`` holds to the
  pinned binary.
* ``docker/witan.Dockerfile`` stamps ``WITAN_HTTP_API_CONTRACTS`` as
  ``edu.mit.ol.omnigraph.http-api-contracts``, a comma-separated list. It must
  equal ``SUPPORTED_HTTP_API_CONTRACTS``.

Both label names are a cross-repo contract with ol-infrastructure's
``image_schema.py``. A runtime stage that doesn't re-declare the ARG expands the
label to the empty string and still builds, so this checks the runtime stage's
ARG and LABEL lines as well as the defaults.
"""

import re
import sys
from pathlib import Path

import cyclopts

from witan_core.omnigraph_http import HTTP_API_CONTRACT, SUPPORTED_HTTP_API_CONTRACTS

app = cyclopts.App(
    name="check-omnigraph-contract-labels",
    help="Assert both images label the HTTP contracts witan-core declares.",
)

#: (Dockerfile, build arg, label, the value witan-core declares)
LABELS = (
    (
        Path("docker/omnigraph-server.Dockerfile"),
        "OMNIGRAPH_HTTP_API_CONTRACT",
        "edu.mit.ol.omnigraph.http-api-contract",
        HTTP_API_CONTRACT,
    ),
    (
        Path("docker/witan.Dockerfile"),
        "WITAN_HTTP_API_CONTRACTS",
        "edu.mit.ol.omnigraph.http-api-contracts",
        ",".join(SUPPORTED_HTTP_API_CONTRACTS),
    ),
)


def problems(
    dockerfile: Path, text: str, arg: str, label: str, declared: str
) -> list[str]:
    """Return every way ``text`` fails to label ``declared`` as ``label``.

    :param dockerfile: the Dockerfile's path, for the messages.
    :param text: the Dockerfile's contents.
    :param arg: the build arg that carries the value.
    :param label: the OCI label the runtime stage emits.
    :param declared: the value witan-core declares.
    :returns: one message per problem; empty when the Dockerfile agrees.
    :rtype: list[str]
    """
    found = []
    default = re.search(rf"^ARG {arg}=(\S+)$", text, re.MULTILINE)
    if default is None:
        found.append(f"{dockerfile} declares no 'ARG {arg}=...' default.")
    elif default.group(1) != declared:
        found.append(
            f"{dockerfile} sets {arg}={default.group(1)}, but witan-core declares "
            f"{declared}. Move them together."
        )
    runtime = re.search(
        r"^FROM .* AS runtime$(.*?)(?=^FROM |\Z)", text, re.MULTILINE | re.DOTALL
    )
    if runtime is None:
        found.append(f"{dockerfile} has no 'FROM ... AS runtime' stage.")
        return found
    stage = runtime.group(1)
    if not re.search(rf"^ARG {arg}$", stage, re.MULTILINE):
        found.append(
            f"{dockerfile}'s runtime stage never re-declares {arg}, so {label} builds empty."
        )
    if f'{label}="${{{arg}}}"' not in stage:
        found.append(
            f"{dockerfile}'s runtime stage does not emit {label} from ${{{arg}}}."
        )
    return found


@app.default
def check(root: Path = Path()) -> None:
    """Check both Dockerfiles under ``root``.

    :param root: the repository root.
    """
    found = [
        problem
        for dockerfile, arg, label, declared in LABELS
        for problem in problems(
            dockerfile, (root / dockerfile).read_text(), arg, label, declared
        )
    ]
    if found:
        print("omnigraph HTTP-contract labels have drifted:", file=sys.stderr)
        for problem in found:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit(1)
    print(
        f"omnigraph HTTP-contract labels agree: server {HTTP_API_CONTRACT}, "
        f"witan {','.join(SUPPORTED_HTTP_API_CONTRACTS)}"
    )


if __name__ == "__main__":
    app()
