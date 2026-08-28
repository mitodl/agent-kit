#!/usr/bin/env -S uv run --quiet --package witan-core --extra cli --with packaging python
"""Report when a real omnigraph release exists at or above the pinned version.

WHY THIS IS NOT RENOVATE'S JOB. Renovate does manage the omnigraph pin
(renovate.json's customManager, datasource ``github-releases``) and has cut
bumps before — #110 to v0.8.1, #216 to v0.9.0. It cannot cover THIS moment,
for two independent reasons:

  * A datasource proposes a version GREATER than ``currentValue``. The pin
    currently declares ``0.10.0`` because that is what the ``edge`` build
    reports, while upstream's newest real release is v0.9.0. So the event that
    matters — upstream finally cutting a v0.10.0 — compares EQUAL to the pin
    and produces no PR at all. The one release we most need to hear about is
    the one Renovate is structurally blind to.

  * The org config (mitodl/.github :: renovate-config) sets
    ``minimumReleaseAge: "14 days"`` and a weekend-only schedule. Even a
    v0.10.1, which Renovate would see, stays silent for a fortnight.

WHAT IT WATCHES FOR, AND WHY THAT MATTERS. witan pins omnigraph to the MOVING
``edge`` tag across three tiers — a deliberate maintainer decision on
2026-08-19, taken rather than falling back to v0.9.0 while waiting for a real
0.10.0. Upstream force-updates ``edge`` on every push to main, docs-only
pushes included, so the pinned digests go stale on upstream's schedule. Three
drifts in one week (#248, #250, #281) were each discovered by a red
``witan-code`` job, and the 08-24 one blocked every open PR in the repo, not
just the one that hit it. A real release is the trigger to stop tracking
``edge`` and end that whole class of toil. Nothing was watching for it; that
it had not happened yet was luck, not observation.

DELIBERATELY NOT A BLOCKING CHECK. Failing every PR the day upstream cuts a
release punishes contributors for an event outside the repo — the same shape
as the drift problem it is meant to relieve. It runs on a schedule and its
output is an issue: information, not a gate.

SELF-RETIRING. When ``_OMNIGRAPH_RELEASE_TAG`` names a real ``v<version>``
release, the moving-tag situation is over and Renovate's ordinary path
applies, so this reports "nothing to watch" and stays quiet without anyone
having to remember to delete it.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import cyclopts
from packaging.version import InvalidVersion, Version
from witan_core.omnigraph_install import (
    _OMNIGRAPH_RELEASE_TAG,
    _OMNIGRAPH_VERSION,
)

app = cyclopts.App(
    name="check-omnigraph-release",
    help="Report whether a real omnigraph release exists at or above the pin.",
)

REPO = "ModernRelay/omnigraph"

#: A tag that pins a real release, as opposed to a moving one like ``edge``.
_REAL_TAG = re.compile(r"^v\d+\.\d+\.\d+")

#: One page is 19 releases' worth of headroom today. The cap is a guard against
#: a malformed Link header looping, not a real expectation of pagination.
_MAX_PAGES = 10

_PINS = """\
  packages/witan-core/witan_core/omnigraph_install.py  (_OMNIGRAPH_VERSION,
      _OMNIGRAPH_RELEASE_TAG, _OMNIGRAPH_ASSET_SHA256)
  docker/omnigraph-server.Dockerfile                   (ARG OMNIGRAPH_*)
  docker/witan.Dockerfile                              (ARG OMNIGRAPH_*)"""


def _get(url: str) -> tuple[bytes, str | None]:
    """Fetch ``url``, returning its body and the ``next`` Link target if any.

    Sends ``GITHUB_TOKEN`` when the environment has one. The repo is public so
    the call works without it, but unauthenticated GitHub API requests are
    rate-limited per source IP, and a hosted runner's IP is shared — an
    anonymous call from CI is the one most likely to be refused.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agent-kit-omnigraph-release-watch",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            link = response.headers.get("Link", "")
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"could not reach the GitHub API for {REPO}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    match = re.search(r'<([^>]+)>;\s*rel="next"', link)
    return body, match.group(1) if match else None


def real_releases() -> dict[Version, str]:
    """Every published non-prerelease, non-draft release, version → tag name.

    Deliberately NOT ``/releases/latest``, which would be one request instead
    of this loop: that endpoint picks the newest by ``created_at``, so a
    patch cut on an older line after a newer minor (a v0.9.1 after v0.10.0)
    would come back as "latest" and read as a regression against the pin.
    Comparison here is by version, which is the question actually being asked.
    """
    url = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
    found: dict[Version, str] = {}

    for _ in range(_MAX_PAGES):
        body, next_url = _get(url)
        try:
            page = json.loads(body)
        except json.JSONDecodeError as exc:
            print(f"unreadable response from the GitHub API: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc

        for release in page:
            if release.get("prerelease") or release.get("draft"):
                continue
            tag = release.get("tag_name", "")
            try:
                found[Version(tag.removeprefix("v"))] = tag
            except InvalidVersion:
                # `edge` and any other non-version tag. Not an error: a moving
                # tag is exactly what this check exists because of.
                continue

        if not next_url:
            break
        url = next_url

    return found


def _issue_body(pinned: Version, version: Version, tag: str) -> str:
    headline = (
        f"Upstream {REPO} has published **{tag}** — the exact version the pin "
        f"already declares, which is why Renovate will never propose it."
        if version == pinned
        else f"Upstream {REPO} has published **{tag}**, newer than the pinned "
        f"`{pinned}`."
    )
    return f"""\
{headline}

witan is tracking the moving `{_OMNIGRAPH_RELEASE_TAG}` tag, which upstream
force-updates on every push to its main branch. That is why the pinned asset
digests go stale on upstream's schedule and CI goes red until someone
refreshes them. A real release is the trigger to stop tracking it.

Renovate's datasource only proposes versions greater than the pin's
`{pinned}`, and the org config holds any bump it does raise for 14 days.

### Follow-through

1. Re-pin all three tiers to `{tag}` together — a split-version deploy is a
   hard outage, since omnigraph refuses a graph written by a different
   storage format in both directions:

{_PINS}

2. Refresh `_OMNIGRAPH_ASSET_SHA256` and the Dockerfiles' `OMNIGRAPH_SHA256_*`
   from this release's `.sha256` assets — which, unlike a moving tag's, will
   not be republished under the reader's feet.
3. `just check-omnigraph-pins` — the three tiers agree.
4. `just check-omnigraph-format` — the release's storage format matches the
   declared `_OMNIGRAPH_INTERNAL_SCHEMA`. If it does not, that is a graph
   rebuild, not a version bump; read the check's own output before editing
   the declaration.

Closing this issue without re-pinning is a fine outcome if the release is not
one to adopt — the check re-opens it on the next release at or above the pin.
"""


@app.default
def check(json_file: Path | None = None) -> None:
    """Compare upstream's real releases against the pinned omnigraph version.

    Exits 0 when there is nothing to do, 1 when a real release at or above the
    pin exists, and 2 when the check itself could not run.

    Parameters
    ----------
    json_file
        Where to write ``{tag, pinned, title, body}`` when the check fires, for
        a workflow to turn into an issue. Omitted, the body is printed instead.
        Structured rather than passed through step outputs on purpose: the tag
        is a string upstream controls, and it must not reach a shell by way of
        a ``${{ }}`` expansion.
    """
    if _REAL_TAG.match(_OMNIGRAPH_RELEASE_TAG):
        print(
            f"_OMNIGRAPH_RELEASE_TAG is {_OMNIGRAPH_RELEASE_TAG}, a real release "
            f"rather than a moving tag — nothing to watch. Renovate manages the "
            f"version pin from here."
        )
        return

    try:
        pinned = Version(_OMNIGRAPH_VERSION)
    except InvalidVersion as exc:
        print(f"_OMNIGRAPH_VERSION is not a version: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    releases = real_releases()
    if not releases:
        # Every release filtered out is far likelier to be a changed API shape
        # than an upstream with no releases, and reporting "nothing to do" for
        # it would be a check that silently stopped checking.
        print(
            f"no non-prerelease {REPO} release parsed as a version — the API "
            f"response shape or the upstream tagging scheme has changed",
            file=sys.stderr,
        )
        raise SystemExit(2)

    newest = max(releases)
    if newest < pinned:
        print(
            f"tracking `{_OMNIGRAPH_RELEASE_TAG}` at {pinned}; upstream's newest "
            f"real release is still {releases[newest]}. Nothing to do."
        )
        return

    tag = releases[newest]
    body = _issue_body(pinned, newest, tag)
    if json_file is not None:
        json_file.write_text(
            json.dumps(
                {
                    "tag": tag,
                    "pinned": str(pinned),
                    # Version-specific so each release gets its own issue, and
                    # so a re-run finds this one instead of filing a duplicate.
                    "title": f"omnigraph {tag} is released — re-pin off `{_OMNIGRAPH_RELEASE_TAG}`",
                    "body": body,
                }
            )
        )
    else:
        print(body)

    print(
        f"omnigraph {releases[newest]} is published and the repo is still on "
        f"`{_OMNIGRAPH_RELEASE_TAG}` — time to re-pin.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    app()
