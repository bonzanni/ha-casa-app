#!/bin/bash
# Usage: ssh <ha-host> -t 'cd /tmp && bash /path/to/mcp_blip/run.sh'
#
# Assumes `claude` CLI is on PATH (it is, inside the casa container's
# engagement workspaces). Run this OUTSIDE the container to avoid polluting
# a real engagement. Requires python3 + aiohttp.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
echo "Spike workspace: $WORK"

cat >"$WORK/.mcp.json" <<JSON
{"mcpServers": {"casa-framework-spike": {
  "type": "http",
  "url": "http://127.0.0.1:8099/mcp/casa-framework-spike"
}}}
JSON

cat >"$WORK/CLAUDE.md" <<MD
# Spike: MCP blip

Call the \`ping_cc\` tool from \`casa-framework-spike\` once. Report the result.
MD

# Start the server.
python3 "$HERE/server.py" &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null || true" EXIT

sleep 1
cd "$WORK"

# Non-interactive one-shot turn. Real CLI sends the turn, hits the blip, then
# exits. Scripted output in the server's stdout will include "RETRY OBSERVED"
# iff the CC client retried.
timeout 30 claude --print --permission-mode bypassPermissions \
  "Call mcp__casa-framework-spike__ping_cc and tell me the result." \
  || echo "CLI exited non-zero (expected on blip)"

echo "---"
echo "Check server stdout above for 'RETRY OBSERVED'."
