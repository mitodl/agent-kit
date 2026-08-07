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
