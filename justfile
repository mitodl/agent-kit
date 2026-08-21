# Task runner for the agent-kit uv workspace.
#
# `uv sync --package X` mutates the ONE shared workspace `.venv` (there is no
# per-package venv in a uv workspace). Testing sibling packages back to back
# in that same venv means package Y's sync can silently change what package
# X's already-passing tests import the next time they run — a real failure
# mode, not a hypothetical one. `--isolated` sidesteps it entirely: each
# `test-*` recipe below builds its own throwaway venv from just that
# package's `[dependency-groups].test`, so no recipe can see another's state,
# and `test-all` runs them all at once (`[parallel]`, native to just — see
# `just --help`) without needing tox/nox to get the same isolation.

set shell := ["bash", "-uc"]

# Every workspace member: `just recipe-name` <-> `packages/name` path.
# Keep in sync with `[tool.uv.workspace].members` in the root pyproject.toml.

# Run agent-config-kit's tests in an isolated venv.
test-agent-config-kit *args:
    uv run --isolated --package agent-config-kit --group test pytest packages/agent-config-kit {{ args }}

# Run ol-agent-kit's (packages/agent-kit) tests in an isolated venv.
test-ol-agent-kit *args:
    uv run --isolated --package ol-agent-kit --group test pytest packages/agent-kit {{ args }}

# Run witan-core's tests in an isolated venv.
test-witan-core *args:
    uv run --isolated --package witan-core --group test pytest packages/witan-core {{ args }}

# Run witan-council's (mcp/servers/witan) tests in an isolated venv.
test-witan-council *args:
    uv run --isolated --package witan-council --group test pytest mcp/servers/witan {{ args }}

# Run witan-code's (mcp/servers/witan-code) tests in an isolated venv.
test-witan-code *args:
    uv run --isolated --package witan-code --group test pytest mcp/servers/witan-code {{ args }}

# Run every workspace package's tests, each in its own isolated venv, in
# parallel. No args forwarding here since each dependency can only be called
# once per recipe — run a `test-<package>` recipe directly to pass pytest args
# (`-k`, `-x`, ...) to a single package.
[parallel]
test-all: test-agent-config-kit test-ol-agent-kit test-witan-core test-witan-council test-witan-code

# Alias for `test-all`.
test: test-all

# Fail if the three omnigraph version pins have drifted apart.
#
# omnigraph uses strict single-version storage: a binary refuses a graph
# written by a different on-disk format, in either direction. So the version
# `witan setup` puts on a developer's PATH, the version baked into the MCP
# tier's image, and the version the deployed data tier runs are not three
# independent choices — they are one, spelled three times. Renovate's custom
# manager bumps all three (renovate.json), but it silently covered only the
# first for a full release cycle, and nothing failed until deploy. This is the
# second belt.
#
# Parsed with awk, not `grep -oP`: -P is a GNU extension the BSD grep on macOS
# does not have, and darwin/arm64 is a supported installer platform
# (_OMNIGRAPH_ASSETS). A check a Mac developer cannot run is a check that only
# ever fails in CI.
check-omnigraph-pins:
    #!/usr/bin/env bash
    set -euo pipefail
    installer=$(awk -F'"' '/^_OMNIGRAPH_VERSION = /{print $2; exit}' packages/witan-core/witan_core/omnigraph_install.py)
    server=$(awk -F= '/^ARG OMNIGRAPH_VERSION=/{print $2; exit}' docker/omnigraph-server.Dockerfile)
    mcp=$(awk -F= '/^ARG OMNIGRAPH_VERSION=/{print $2; exit}' docker/witan.Dockerfile)
    # The TAG is checked alongside the version, and for the same reason: it
    # decides which upstream build each tier actually downloads, so a partial
    # bump ships a client and a server on different binaries just as surely.
    # Adding a second pin without extending this check would recreate exactly
    # the gap the comment above describes.
    installer_tag=$(awk -F'"' '/^_OMNIGRAPH_RELEASE_TAG = /{print $2; exit}' packages/witan-core/witan_core/omnigraph_install.py)
    server_tag=$(awk -F= '/^ARG OMNIGRAPH_RELEASE_TAG=/{print $2; exit}' docker/omnigraph-server.Dockerfile)
    mcp_tag=$(awk -F= '/^ARG OMNIGRAPH_RELEASE_TAG=/{print $2; exit}' docker/witan.Dockerfile)
    # ★ AND THE DIGEST, which is the only one of the three that actually pins a
    # BUILD. `edge` is force-updated on every push to upstream main, so equal
    # tags prove nothing: each tier can resolve the same tag to a different
    # commit and every check above still passes, leaving a load-test result
    # impossible to attribute. The linux/x86_64 digest is the one all three
    # tiers share, so it is the one compared here.
    installer_sha=$(awk '/^_OMNIGRAPH_ASSET_SHA256/{f=1} f && /omnigraph-linux-x86_64/{getline; gsub(/[^0-9a-f]/,""); print; exit}' packages/witan-core/witan_core/omnigraph_install.py)
    server_sha=$(awk -F= '/^ARG OMNIGRAPH_SHA256_X86_64=/{print $2; exit}' docker/omnigraph-server.Dockerfile)
    mcp_sha=$(awk -F= '/^ARG OMNIGRAPH_SHA256_X86_64=/{print $2; exit}' docker/witan.Dockerfile)
    # An empty capture means the line moved or was renamed, not that the pins
    # agree — three empty strings would otherwise compare equal and pass.
    for pair in "installer:$installer" "server:$server" "mcp:$mcp" \
                "installer_tag:$installer_tag" "server_tag:$server_tag" "mcp_tag:$mcp_tag" \
                "installer_sha:$installer_sha" "server_sha:$server_sha" "mcp_sha:$mcp_sha"; do
        if [[ -z "${pair#*:}" ]]; then
            echo "could not read the omnigraph pin for '${pair%%:*}' — the declaration moved or was renamed" >&2
            exit 1
        fi
    done
    if [[ "$installer" != "$server" || "$installer" != "$mcp" ]]; then
        echo "omnigraph version pins have drifted:" >&2
        echo "  packages/witan-core/witan_core/omnigraph_install.py: $installer" >&2
        echo "  docker/omnigraph-server.Dockerfile:                  $server" >&2
        echo "  docker/witan.Dockerfile:                             $mcp" >&2
        exit 1
    fi
    if [[ "$installer_tag" != "$server_tag" || "$installer_tag" != "$mcp_tag" ]]; then
        echo "omnigraph release-tag pins have drifted:" >&2
        echo "  packages/witan-core/witan_core/omnigraph_install.py: $installer_tag" >&2
        echo "  docker/omnigraph-server.Dockerfile:                  $server_tag" >&2
        echo "  docker/witan.Dockerfile:                             $mcp_tag" >&2
        exit 1
    fi
    if [[ "$installer_sha" != "$server_sha" || "$installer_sha" != "$mcp_sha" ]]; then
        echo "omnigraph linux/x86_64 digest pins have drifted — the tiers would" >&2
        echo "install DIFFERENT builds even though the version and tag agree:" >&2
        echo "  packages/witan-core/witan_core/omnigraph_install.py: $installer_sha" >&2
        echo "  docker/omnigraph-server.Dockerfile:                  $server_sha" >&2
        echo "  docker/witan.Dockerfile:                             $mcp_sha" >&2
        exit 1
    fi
    echo "omnigraph pins agree: $installer (tag $installer_tag, sha ${installer_sha:0:12}…)"

# Fail if the pinned omnigraph binary reads a storage format this repo does not
# declare — i.e. if a version bump is secretly a rebuild-every-graph event.
#
# Needs the pinned binary on PATH (`witan setup`, or the workflow's install
# step). See bin/check_omnigraph_format.py for why this compares the binary
# against a declaration rather than diffing two release pins.
#
# `--extra cli` is load-bearing: cyclopts (and rich, which the installer imports
# lazily) live in witan-core's `cli` extra, not its base dependencies. Without
# it this works only in a shared workspace .venv that some other package's sync
# happened to leave cyclopts in — which is exactly how it passed locally and
# failed in CI the first time.
check-omnigraph-format *args:
    uv run --package witan-core --extra cli python bin/check_omnigraph_format.py {{ args }}

# Runs in CI on every PR, so drift is caught however the bump was made — which
# matters, because the previous drift happened by NOT using the tool: three of
# five packages had a `[tool.bumpversion].current_version` stranded several
# releases behind the real version, so `bump-my-version` searched for a string
# that no longer existed and silently did nothing.

# Fail if any package's version, bumpversion config, and CHANGELOG disagree.
check-versions *args:
    uv run --package witan-core --extra cli python bin/check_versions.py {{ args }}

# Fail if a server imports a witan_core symbol its declared floor predates.
#
# NEEDS NETWORK: it asks PyPI which witan-core versions exist, then installs the
# lowest one the floor admits into a throwaway venv. That is the point — the
# workspace resolves witan-core BY PATH, so any check that stays inside it
# proves nothing. See bin/check_core_floor.py for why the pin has to be `==`
# and why an unpublished floor is a pass, not a failure.
#
# Slower than the other checks (two wheel builds, two venvs, two installs), so
# it is its own CI job rather than a step inside another.
check-core-floor *args:
    uv run --package witan-core --extra cli --with packaging python bin/check_core_floor.py {{ args }}

# CHANGELOG FIRST, THEN THE VERSION — and the order is the whole point. The
# recipe computes the version this bump would produce and refuses unless
# CHANGELOG.md already has a `## [<that version>]` heading. So the only way to
# release is to have written down what is being released, and the two cannot
# drift apart, because the bump will not run until they agree.
#
# Publishing is triggered by a push to main touching the package's own
# pyproject.toml (.github/workflows/publish-*.yml), so this recipe IS the
# release. It deliberately does not commit, tag, or push: what to say in the
# commit is a judgement call, and a recipe that pushed on your behalf would be
# one typo away from an unintended PyPI release.
#
# bump-my-version comes via uvx rather than a dev dependency — it is needed
# only at release time, and pinning it into the workspace would put it in every
# contributor's environment for no benefit. It IS version-pinned, though: this
# recipe parses `show-bump`'s human-readable output, so an unpinned `uvx` could
# resolve a release that reformats it and break version calculation during a
# release. One variable, used for both the preview and the mutation, so those
# two can never run different versions of the tool.

# Release a package, e.g. `just bump witan-core minor`. Changelog entry first.
bump package part:
    #!/usr/bin/env bash
    set -euo pipefail
    BUMP_TOOL="bump-my-version@1.4.1"   # keep in step with AGENTS.md
    case "{{ package }}" in
        witan-core)       dir=packages/witan-core ;;
        witan-council)    dir=mcp/servers/witan ;;
        witan-code)       dir=mcp/servers/witan-code ;;
        agent-config-kit) dir=packages/agent-config-kit ;;
        ol-agent-kit)     dir=packages/agent-kit ;;
        *) echo "unknown package '{{ package }}' — one of: witan-core witan-council witan-code agent-config-kit ol-agent-kit" >&2; exit 1 ;;
    esac
    case "{{ part }}" in
        major|minor|patch) ;;
        *) echo "part must be major, minor or patch (got '{{ part }}')" >&2; exit 1 ;;
    esac

    # Everything must already agree before touching anything: bumping on top of
    # existing drift would bury the drift in a release rather than fix it.
    just check-versions

    # `show-bump --ascii` renders as `0.15.0 -- bump -+- minor - 0.16.0`, so the
    # part name and its resulting version sit on one line. Asking the tool
    # rather than computing it here keeps this honest if the versioning scheme
    # ever grows a pre-release component — but it also means this recipe is
    # coupled to a human-readable format, which is why the tool is PINNED
    # (BUMP_TOOL, matching AGENTS.md): an unpinned `uvx` would resolve whatever
    # is newest on the day and could reformat this output from under us, at
    # release time, which is the worst moment to discover it.
    #
    # `|| true` and a separate emptiness check, NOT a bare assignment: under
    # `set -euo pipefail` a `grep` that matches nothing exits 1, pipefail
    # propagates it, and the command substitution takes the whole recipe down
    # before the diagnostic below can run. With stderr suppressed as well, the
    # releaser would see a bare non-zero exit and nothing else.
    raw=$(cd "$dir" && uvx "$BUMP_TOOL" show-bump --ascii 2>&1) || true
    new=$(printf '%s\n' "$raw" \
        | grep -oE "\b{{ part }} - [0-9]+\.[0-9]+\.[0-9]+" \
        | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -1) || true
    if [[ -z "$new" ]]; then
        echo "could not determine the {{ part }} version for {{ package }}." >&2
        echo "\`$BUMP_TOOL show-bump\` said:" >&2
        printf '%s\n' "$raw" >&2
        exit 1
    fi

    if ! grep -qE "^## \[${new//./\\.}\]" "$dir/CHANGELOG.md"; then
        echo "" >&2
        echo "$dir/CHANGELOG.md has no entry for ${new}." >&2
        echo "" >&2
        echo "Write it first — a release with no changelog entry is one nobody" >&2
        echo "can read. Add a '## [${new}] - $(date +%F)' section, then re-run:" >&2
        echo "  just bump {{ package }} {{ part }}" >&2
        exit 1
    fi

    (cd "$dir" && uvx "$BUMP_TOOL" bump "{{ part }}" --no-commit --no-tag)
    just check-versions
    echo ""
    echo "{{ package }} bumped to ${new}. Commit together:"
    echo "  $dir/pyproject.toml"
    echo "  $dir/CHANGELOG.md"
    # uv.lock records every workspace member's version, so it moves with the
    # bump — `just check-versions` above runs uv and refreshes it. Left behind,
    # the next `uv lock --check` (and CI) fails on a repo that looks untouched.
    echo "  uv.lock"
