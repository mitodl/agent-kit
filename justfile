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
check-omnigraph-pins:
    #!/usr/bin/env bash
    set -euo pipefail
    installer=$(grep -oP '_OMNIGRAPH_VERSION = "\K[^"]+' packages/witan-core/witan_core/omnigraph_install.py)
    server=$(grep -oP '^ARG OMNIGRAPH_VERSION=\K\S+' docker/omnigraph-server.Dockerfile)
    mcp=$(grep -oP '^ARG OMNIGRAPH_VERSION=\K\S+' docker/witan.Dockerfile)
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
check-omnigraph-format *args:
    uv run --package witan-core python bin/check_omnigraph_format.py {{ args }}
