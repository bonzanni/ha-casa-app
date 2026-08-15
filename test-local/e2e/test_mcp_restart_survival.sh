#!/usr/bin/env bash
# Plan 4b/3.6 — mid-restart survival e2e test.
#
# Validates the headline guarantee of v0.14.0: bouncing casa-main
# (s6-rc -d/-u change svc-casa) does NOT take svc-casa-mcp down or sever
# the tool-call surface. Mid-restart tool calls return JSON-RPC
# -32000 casa_temporarily_unavailable; post-restart calls succeed.
#
# WHICH TOOL THE PROBE CALLS, AND WHY (#585)
# ------------------------------------------
# Every call below is UNBOUND — it carries no X-Casa-Engagement-Id/-Token
# pair, because this probe provisions no engagement. Since v0.166.0 the
# bridge grant-gate fails closed for an unbound caller
# (`engagement_casa_grant_names(None)` returns an empty set, tools.py), so
# `list_engagement_workspaces` — what M-2 used to call — is refused -32004
# before it runs. That refusal is CORRECT: a real /mcp/casa-framework tool
# call comes from an engagement workspace, whose .mcp.json bakes the header
# pair (workspace.py), so no legitimate NON-TERMINAL tool call arrives on
# this route unbound. (Unbound completion retries do — that is the
# exemption below. Other routes on 8100 authenticate differently: the
# /internal/channel/* family carries its token in the body.) The probe
# simply predated the gate, and had been failing every scheduled run since
# 2026-08-09.
#
# The round-trip steps therefore call `emit_completion`, the one tool the
# gate deliberately exempts (_TERMINAL_BINDING_TOOLS, for completion
# retries that legitimately arrive unbound). With no engagement it returns
# a dispatched `not_in_engagement` result and touches nothing else
# (tools.py — it returns before validation, registry access or
# finalization), so M-2/M-6 still prove the WHOLE path: forwarder → Unix
# socket → casa-main's handler → `await fn(arguments)` → response. Pinned
# unit-side by test_tools_call_unbound_emit_completion_still_dispatches.
#
# M-2b/M-7 pin the gate itself, so the behaviour that broke this probe is
# now asserted rather than merely worked around — and M-7 additionally
# shows a casa-main restart does not reopen it.
#
# WHAT THIS PROBE DOES NOT COVER
# ------------------------------
# It does not exercise a real engagement CLI across the bounce: no
# authenticated call, no surviving subprocess, no reused MCP connection.
# Closing that gap needs a provisioned mock-CLI engagement driving the
# calls itself; tracked in #586.
#
# Do NOT close it by making an authenticated post-bounce call pass as-is.
# Boot reconcile lands every reloaded ACTIVE record in `idle`
# (engagement_registry.py; terminal records stay terminal), and for a
# NON-TERMINAL tool the handler binds only `active`
# (internal_handlers.py) — so an authenticated non-terminal call is
# refused -32004 today, and that refusal is a DEFECT, not a contract to
# assert: the inbound spool can redeliver a queued turn to the respawned
# CLI without anything flipping the record back to `active` (#588). A
# probe written against today's behaviour would pin the bug. (The steps
# above cannot show it: `emit_completion` is exempt from both the status
# binding and the grant gate, so it answers the same either way.)
#
# Mock-CLI gated (CASA_USE_MOCK_CLAUDE=1). Auto-skips otherwise.

set -euo pipefail

if [ "${CASA_USE_MOCK_CLAUDE:-0}" != "1" ]; then
    echo "SKIP: CASA_USE_MOCK_CLAUDE=1 required"
    exit 0
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/common.sh"

build_image_with_mock_cli

NAME="casa-mcp-restart-$$"
start_container "$NAME"
trap "stop_container '$NAME' >/dev/null 2>&1 || true" EXIT

wait_healthy "$NAME"

echo "=== M-1: confirm svc-casa-mcp is bound on 8100 ==="
RESP=$(MSYS_NO_PATHCONV=1 docker exec "$NAME" curl -fsSL -X POST \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
    http://127.0.0.1:8100/mcp/casa-framework)
echo "  initialize: $RESP"
echo "$RESP" | python3 -c 'import json, sys; d = json.load(sys.stdin); assert d.get("result", {}).get("serverInfo", {}).get("name") == "casa-framework", d' \
    || fail "M-1 svc-casa-mcp not responding on 8100"
pass "M-1 svc-casa-mcp bound on 8100"

# Assert a tools/call response is a DISPATCHED emit_completion result — not
# merely a JSON-RPC reply. `.result` alone would be satisfied by anything the
# forwarder itself can answer, so this decodes the content block and checks
# the tool's own payload: only casa-main running the handler produces it.
assert_dispatched() {
    local label="$1" resp="$2"
    echo "$resp" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("result") is not None, d
payload = json.loads(d["result"]["content"][0]["text"])
assert payload.get("kind") == "not_in_engagement", payload
' || fail "$label expected a dispatched emit_completion result; got: $resp"
}

# Assert the grant-gate refused an unbound non-terminal tool (#585).
assert_gated() {
    local label="$1" resp="$2"
    echo "$resp" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("error", {}).get("code") == -32004, d
assert "tool_not_granted" in d["error"]["message"], d
' || fail "$label expected -32004 tool_not_granted; got: $resp"
}

call_tool() {   # call_tool <id> <tool-name>
    MSYS_NO_PATHCONV=1 docker exec "$NAME" curl -fsSL -X POST \
        -H "Content-Type: application/json" \
        -d "{\"jsonrpc\":\"2.0\",\"id\":$1,\"method\":\"tools/call\",\"params\":{\"name\":\"$2\",\"arguments\":{}}}" \
        http://127.0.0.1:8100/mcp/casa-framework
}

echo "=== M-2: tool call before bounce — expect a dispatched result ==="
PRE=$(call_tool 2 emit_completion)
echo "  pre-bounce: $PRE"
assert_dispatched "M-2" "$PRE"
pass "M-2 pre-bounce tool call dispatched in casa-main"

echo "=== M-2b: unbound non-terminal tool — expect the grant-gate refusal ==="
GATED=$(call_tool 21 list_engagement_workspaces)
echo "  unbound: $GATED"
assert_gated "M-2b" "$GATED"
pass "M-2b unbound non-terminal tool refused -32004 (gate live)"

echo "=== M-3: bounce casa-main (svc-casa down) ==="
MSYS_NO_PATHCONV=1 docker exec "$NAME" s6-rc -d change svc-casa
sleep 1

echo "=== M-4: tool call during bounce — expect -32000 ==="
DOWN=$(call_tool 3 emit_completion)
echo "  during-bounce: $DOWN"
echo "$DOWN" | python3 -c 'import json, sys; d = json.load(sys.stdin); assert d.get("error", {}).get("code") == -32000, d' \
    || fail "M-4 expected error.code -32000; got: $DOWN"
pass "M-4 mid-bounce returned casa_temporarily_unavailable"

echo "=== M-5: bring casa-main back up + wait for socket ==="
MSYS_NO_PATHCONV=1 docker exec "$NAME" s6-rc -u change svc-casa
# Poll for casa-main's Unix socket to come back (up to 12s).
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if MSYS_NO_PATHCONV=1 docker exec "$NAME" test -S /run/casa/internal.sock; then
        echo "  socket ready after ${i}s"
        break
    fi
    sleep 1
done

echo "=== M-6: tool call after bounce — expect a dispatched result ==="
POST=$(call_tool 4 emit_completion)
echo "  post-bounce: $POST"
assert_dispatched "M-6" "$POST"
pass "M-6 post-bounce tool call dispatched in casa-main"

echo "=== M-7: gate still closed after the restart ==="
POST_GATED=$(call_tool 41 list_engagement_workspaces)
echo "  post-bounce unbound: $POST_GATED"
assert_gated "M-7" "$POST_GATED"
pass "M-7 restart did not reopen the grant-gate"

echo "=== ALL PASS — mcp_restart_survival ==="
