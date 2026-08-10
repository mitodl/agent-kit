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
    # An empty capture means the line moved or was renamed, not that the pins
    # agree — three empty strings would otherwise compare equal and pass.
    for pair in "installer:$installer" "server:$server" "mcp:$mcp"; do
        if [[ -z "${pair#*:}" ]]; then
            echo "could not read the omnigraph pin for '${pair%%:*}' — the declaration moved or was renamed" >&2
            exit 1
        fi
    done
    if [[ "$installer" == "$server" && "$installer" == "$mcp" ]]; then
        echo "omnigraph pins agree: $installer"
    else
        echo "omnigraph version pins have drifted:" >&2
        echo "  packages/witan-core/witan_core/omnigraph_install.py: $installer" >&2
        echo "  docker/omnigraph-server.Dockerfile:                  $server" >&2
        echo "  docker/witan.Dockerfile:                             $mcp" >&2
        exit 1
    fi

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
# contributor's environment for no benefit.

# Release a package, e.g. `just bump witan-core minor`. Changelog entry first.
bump package part:
    #!/usr/bin/env bash
    set -euo pipefail
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
    # ever grows a pre-release component.
    new=$(cd "$dir" && uvx bump-my-version show-bump --ascii 2>/dev/null \
        | grep -oE "\b{{ part }} - [0-9]+\.[0-9]+\.[0-9]+" \
        | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -1)
    if [[ -z "$new" ]]; then
        echo "could not determine the {{ part }} version for {{ package }}" >&2
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

    (cd "$dir" && uvx bump-my-version bump "{{ part }}" --no-commit --no-tag)
    just check-versions
    echo "{{ package }} bumped to ${new} — commit pyproject.toml + CHANGELOG.md together."
