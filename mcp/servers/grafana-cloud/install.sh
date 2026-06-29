#!/usr/bin/env bash
# install.sh — Wire the Grafana Cloud MCP server(s) into your agent config.
#
# Grafana Cloud MCP is a remote, hosted server (not a local process):
#   - Endpoint:   https://mcp.grafana.com/mcp
#   - Transport:  Streamable HTTP only (no stdio / no SSE)
#   - Auth:       OAuth 2.1 — a browser consent flow runs on first connect,
#                 so there is NO token or API key to configure here.
#
# Each MCP server entry is pinned to ONE Grafana stack via the `X-Grafana-URL`
# header. We have three stacks, so this registers three separately-named
# servers (grafana-ci / grafana-qa / grafana-prod), each with its own OAuth.
#
#   ci    -> https://mitolci.grafana.net
#   qa    -> https://mitolqa.grafana.net
#   prod  -> https://mitolproduction.grafana.net
#
# Usage:
#   ./install.sh                       # all three stacks, agent=claude
#   ./install.sh --agent claude        # explicit agent
#   ./install.sh --instance prod       # just one stack (ci | qa | prod)
#
# Docs: https://grafana.com/docs/grafana-cloud/machine-learning/assistant/configure/cloud-mcp/
set -euo pipefail

ENDPOINT="https://mcp.grafana.com/mcp"
AGENT="claude"
INSTANCE="all"

# Parallel arrays (macOS bash 3.2 has no associative arrays).
INSTANCE_KEYS=(ci qa prod)
INSTANCE_URLS=(
	"https://mitolci.grafana.net"
	"https://mitolqa.grafana.net"
	"https://mitolproduction.grafana.net"
)

while [[ $# -gt 0 ]]; do
	case "$1" in
		--agent)
			AGENT="$2"
			shift 2
			;;
		--instance)
			INSTANCE="$2"
			shift 2
			;;
		*)
			echo "Unknown argument: $1" >&2
			exit 1
			;;
	esac
done

# Resolve the X-Grafana-URL for an instance key; empty if unknown.
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
			gurl="$(url_for "$key")"
			name="grafana-${key}"
			echo "==> Adding ${name} -> ${gurl} ..."
			# NB: name + URL come BEFORE --header. `--header` is variadic, so if it
			# precedes the URL the parser swallows the URL as another header value
			# ("missing required argument 'commandOrUrl'").
			claude mcp add "$name" "$ENDPOINT" \
				--transport http \
				--header "X-Grafana-URL: ${gurl}"
		done
		echo ""
		echo "Done. On first use of each server, Claude opens a browser for the"
		echo "Grafana OAuth consent flow (once per stack)."
		echo "Run '/mcp' inside Claude Code (or 'claude mcp list') to confirm and authenticate."
		;;
	copilot | vscode)
		echo "==> VS Code / GitHub Copilot uses a JSON config file, not a CLI."
		echo "    Copy config/copilot.json into .vscode/mcp.json (or your user mcp.json)."
		echo "    It already contains all three stacks (grafana-ci / -qa / -prod)."
		;;
	pi)
		echo "==> pi uses a JSON config file."
		echo "    Copy config/pi.json into ~/.pi/agent/mcp.json."
		echo "    It already contains all three stacks (grafana-ci / -qa / -prod)."
		;;
	*)
		echo "Unknown agent: ${AGENT} (expected: claude | copilot | pi)" >&2
		exit 1
		;;
esac
