#!/usr/bin/env -S uv run --quiet --package witan-core python
"""Fail when the omnigraph-server image's patched CLI drifts from the release pin.

The image normally ships both omnigraph binaries from the release tarball,
which ``just check-omnigraph-pins`` pins by digest. The one exception is the
CLI built by the Dockerfile's ``cli-patched`` stage (see the
``OMNIGRAPH_CLI_SOURCE`` comment there). That binary has no digest to compare,
so this check holds its inputs to the pin instead:

* ``OMNIGRAPH_CLI_SOURCE`` is ``patched`` exactly when ``OMNIGRAPH_RELEASE_TAG``
  equals ``OMNIGRAPH_CLI_PATCH_BASE_TAG``. A patched CLI from a different
  release than the server reads a different storage format (strict
  single-version storage), and a stock CLI on the patch's release cannot reach
  the S3 cluster root, which is the reason the patch exists. Either mismatch
  is a partial bump.
* The base commit is a full commit id and the stage fetches by it, so a moved
  tag cannot change the build.
* The runtime stage installs ``omnigraph`` once, from the selected stage, and
  every patch the Dockerfile copies exists. Otherwise the selector could read
  ``patched`` while the image still ships the stock binary, or the build fails
  only in Concourse.

A Dockerfile that declares none of the three CLI pins and expands none of them
has dropped the patch and passes. Declaring some but not all is an error.
"""

import re
import sys
from pathlib import Path

import cyclopts

app = cyclopts.App(
    name="check-omnigraph-cli-patch",
    help="Assert the omnigraph-server image's patched CLI agrees with the release pin.",
)

DOCKERFILE = Path("docker/omnigraph-server.Dockerfile")
CLI_PINS = (
    "OMNIGRAPH_CLI_SOURCE",
    "OMNIGRAPH_CLI_PATCH_BASE_TAG",
    "OMNIGRAPH_CLI_PATCH_BASE_COMMIT",
)
CLI_SOURCES = ("release", "patched")
SELECTED_STAGE = "FROM cli-${OMNIGRAPH_CLI_SOURCE} AS cli"
RUNTIME_COPY = "COPY --from=cli /out/omnigraph /usr/local/bin/omnigraph"
FETCH_BY_COMMIT = 'git fetch -q --depth 1 https://github.com/ModernRelay/omnigraph.git "${OMNIGRAPH_CLI_PATCH_BASE_COMMIT}"'


def _arg_defaults(text: str) -> dict[str, str]:
    return dict(re.findall(r"^ARG (\w+)=(\S+)$", text, flags=re.MULTILINE))


def _runtime_stage(text: str) -> str:
    match = re.search(
        r"^FROM .* AS runtime$(.*?)(?=^FROM |\Z)", text, re.MULTILINE | re.DOTALL
    )
    if match is None:
        raise SystemExit(f"{DOCKERFILE} has no 'FROM ... AS runtime' stage.")
    return match.group(1)


def problems(text: str, root: Path) -> list[str]:
    """Return every way ``text`` (the Dockerfile) breaks the patched-CLI rules.

    :param text: the Dockerfile's contents.
    :param root: the repository root that ``COPY`` sources resolve against.
    :returns: one message per problem; empty when the Dockerfile agrees.
    :rtype: list[str]
    """
    args = _arg_defaults(text)
    declared = [name for name in CLI_PINS if name in args]
    if not declared:
        if "${OMNIGRAPH_CLI_" in text:
            return [
                (
                    "declares no patched-CLI pins but still expands one (e.g. the "
                    "cli-${OMNIGRAPH_CLI_SOURCE} selector), which builds an empty value; drop "
                    "the cli-patched stage, the selector and the cli-source label together."
                )
            ]
        return []
    if len(declared) != len(CLI_PINS):
        missing = ", ".join(name for name in CLI_PINS if name not in args)
        return [
            f"declares some patched-CLI pins but not {missing}; set all three or drop all three."
        ]

    found = []
    source = args["OMNIGRAPH_CLI_SOURCE"]
    release_tag = args["OMNIGRAPH_RELEASE_TAG"]
    base_tag = args["OMNIGRAPH_CLI_PATCH_BASE_TAG"]
    if source not in CLI_SOURCES:
        found.append(
            f"OMNIGRAPH_CLI_SOURCE={source} is not one of {', '.join(CLI_SOURCES)}."
        )
    elif source == "patched" and release_tag != base_tag:
        found.append(
            f"OMNIGRAPH_CLI_SOURCE=patched builds the CLI from {base_tag}, but the server ships "
            f"{release_tag}. Rebase docker/patches/ onto {release_tag} and move "
            "OMNIGRAPH_CLI_PATCH_BASE_TAG/_COMMIT, or set OMNIGRAPH_CLI_SOURCE=release if the "
            "release carries the fix."
        )
    elif source == "release" and release_tag == base_tag:
        found.append(
            f"OMNIGRAPH_RELEASE_TAG is {release_tag}, the patch's base, but OMNIGRAPH_CLI_SOURCE="
            "release ships the stock CLI, which cannot address the S3 cluster root. Set "
            "OMNIGRAPH_CLI_SOURCE=patched in the same change as the bump."
        )
    if not re.fullmatch(r"[0-9a-f]{40}", args["OMNIGRAPH_CLI_PATCH_BASE_COMMIT"]):
        found.append(
            "OMNIGRAPH_CLI_PATCH_BASE_COMMIT must be a full lowercase commit id."
        )
    if SELECTED_STAGE not in text:
        found.append(f"no '{SELECTED_STAGE}' line, so the selector picks nothing.")
    if FETCH_BY_COMMIT not in text:
        found.append(
            f"no '{FETCH_BY_COMMIT}', so the patched stage does not build the pinned commit."
        )
    runtime = _runtime_stage(text)
    cli_copies = re.findall(r"^COPY .*/usr/local/bin/omnigraph$", runtime, re.MULTILINE)
    if cli_copies != [RUNTIME_COPY]:
        found.append(
            f"the runtime stage must install /usr/local/bin/omnigraph by exactly '{RUNTIME_COPY}' "
            f"(found {cli_copies}), or the selector is ignored."
        )
    if not re.search(r"^ARG OMNIGRAPH_CLI_SOURCE$", runtime, re.MULTILINE):
        found.append(
            "the runtime stage does not re-declare OMNIGRAPH_CLI_SOURCE, so its label builds empty."
        )
    patches = re.findall(r"^COPY (docker/patches/\S+)", text, re.MULTILINE)
    if not patches:
        found.append(
            "no 'COPY docker/patches/...' line, so the patched stage applies nothing."
        )
    found.extend(
        f"{patch} does not exist." for patch in patches if not (root / patch).is_file()
    )
    return found


@app.default
def check(root: Path = Path()) -> None:
    """Check the omnigraph-server Dockerfile under ``root``.

    :param root: the repository root.
    """
    text = (root / DOCKERFILE).read_text()
    found = problems(text, root)
    if found:
        print(
            f"{DOCKERFILE}: the patched omnigraph CLI has drifted from the release pin:",
            file=sys.stderr,
        )
        for problem in found:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit(1)
    args = _arg_defaults(text)
    if "OMNIGRAPH_CLI_SOURCE" in args:
        print(
            f"omnigraph-server CLI: {args['OMNIGRAPH_CLI_SOURCE']} "
            f"(patch base {args['OMNIGRAPH_CLI_PATCH_BASE_TAG']}, server {args['OMNIGRAPH_RELEASE_TAG']})"
        )
    else:
        print("omnigraph-server CLI: release (no patch declared)")


if __name__ == "__main__":
    app()
