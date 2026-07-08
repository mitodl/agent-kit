#!/usr/bin/env bash
# install.sh — Wire the ToolHive SWE MCP server(s) into your agent config.
#
# ToolHive SWE MCP is a remote, hosted server (not a local process). We run
# one installation per environment tier, each at its own hostname:
#
#   ci    -> https://toolhive-swe.ci.ol.mit.edu/mcp
#   qa    -> https://toolhive-swe.qa.ol.mit.edu/mcp
#   prod  -> https://toolhive-swe.ol.mit.edu/mcp
#
#   - Transport:  Streamable HTTP only (no stdio / no SSE)
#   - Auth:       OAuth 2.1 — a browser consent flow runs on first connect,
#                 so there is NO token or API key to configure here. You must
#                 have an account in the `ol-platform-engineering` realm of
#                 Keycloak for the tier you're connecting to.
#
# By default the server is registered at USER scope so it is available from any
# repo / directory you work in (not just the current project). Pass
# `--scope project` to share it via a project-local `.mcp.json`, or
# `--scope local` for the old project-private behavior.
#
# Usage:
#   ./install.sh                       # all three tiers, agent=claude, scope=user
#   ./install.sh --agent claude        # explicit agent
#   ./install.sh --instance prod       # just one tier (ci | qa | prod)
#   ./install.sh --scope project       # share via project .mcp.json instead
set -euo pipefail

AGENT="claude"
INSTANCE="all"
SCOPE="user"

# Parallel arrays (macOS bash 3.2 has no associative arrays).
INSTANCE_KEYS=(ci qa prod)
INSTANCE_URLS=(
	"https://toolhive-swe.ci.ol.mit.edu/mcp"
	"https://toolhive-swe.qa.ol.mit.edu/mcp"
	"https://toolhive-swe.ol.mit.edu/mcp"
)

while [[ $# -gt 0 ]]; do
	case "$1" in
		--agent)
			[[ $# -ge 2 ]] || { echo "Missing value for --agent" >&2; exit 1; }
			AGENT="$2"
			shift 2
			;;
		--instance)
			[[ $# -ge 2 ]] || { echo "Missing value for --instance" >&2; exit 1; }
			INSTANCE="$2"
			shift 2
			;;
		--scope)
			[[ $# -ge 2 ]] || { echo "Missing value for --scope" >&2; exit 1; }
			SCOPE="$2"
			shift 2
			;;
		*)
			echo "Unknown argument: $1" >&2
			exit 1
			;;
	esac
done

# Resolve the endpoint URL for an instance key; empty if unknown.
url_for() {
	local key="$1" i
	for i in "${!INSTANCE_KEYS[@]}"; do
		if [[ "${INSTANCE_KEYS[$i]}" == "$key" ]]; then
			printf '%s' "${INSTANCE_URLS[$i]}"
			return 0
		fi
	done
	return 1
}

# Build the list of instance keys to install.
keys_to_install=()
if [[ "$INSTANCE" == "all" ]]; then
	keys_to_install=("${INSTANCE_KEYS[@]}")
else
	if ! url_for "$INSTANCE" >/dev/null; then
		echo "Unknown instance: ${INSTANCE} (expected: ci | qa | prod | all)" >&2
		exit 1
	fi
	keys_to_install=("$INSTANCE")
fi

case "$AGENT" in
	claude)
		if ! command -v claude &>/dev/null; then
			echo "==> 'claude' CLI not found on PATH." >&2
			echo "    Install Claude Code, or paste config/claude.json into your config by hand." >&2
			exit 1
		fi
		for key in "${keys_to_install[@]}"; do
			url="$(url_for "$key")"
			name="toolhive-swe-${key}"
			echo "==> Adding ${name} -> ${url} ..."
			claude mcp add "$name" "$url" \
				--scope "$SCOPE" \
				--transport http
		done
		echo ""
		echo "Done (scope: ${SCOPE}). On first use of each server, Claude opens a"
		echo "browser for the Keycloak OAuth consent flow (once per tier). You must"
		echo "have an account in the 'ol-platform-engineering' realm for that tier."
		echo "Run '/mcp' inside Claude Code (or 'claude mcp list') to confirm and authenticate."
		;;
	copilot | vscode)
		echo "==> VS Code / GitHub Copilot uses a JSON config file, not a CLI."
		echo "    Copy config/copilot.json into .vscode/mcp.json (or your user mcp.json)."
		echo "    It already contains all three tiers (toolhive-swe-ci / -qa / -prod)."
		;;
	pi)
		echo "==> pi uses a JSON config file."
		echo "    Copy config/pi.json into ~/.pi/agent/mcp.json."
		echo "    It already contains all three tiers (toolhive-swe-ci / -qa / -prod)."
		;;
	*)
		echo "Unknown agent: ${AGENT} (expected: claude | copilot | vscode | pi)" >&2
		exit 1
		;;
esac
