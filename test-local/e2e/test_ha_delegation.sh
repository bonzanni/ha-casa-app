#!/usr/bin/env bash
# test_ha_delegation.sh — verify Tina's eager Home Assistant facade over the
# real HTTP MCP transport without requiring live model reasoning.
#
# Tier: 2 (functional). Runs on every push and PR.
#
# H-0  Boot mock HA MCP + Casa addon container with CASA_HA_MCP_URL override.
# H-1  Assert addon log shows `Registered Home Assistant MCP server (url=<mock>)`.
# H-2  Start the shipped facade once, then assert direct off/on need exactly
#      one action each and no post-connect tools/list; state needs one
#      GetLiveContext with upstream {}; malformed discovery keeps actions.
# H-3  Raw-fallback harness: docker exec a Python script that loads
#      butler via agent_loader and constructs SDK options like casa_core does;
#      run a query; assert mock HA /_calls gained another entry. Validates
#      the documented degraded raw registry→options path end-to-end.
#
# Coverage scope (per spec §4.2 / plan F.1.0):
# - This test does NOT exercise Ellen→delegate_to_agent→butler reasoning.
#   That two-hop chain requires the in-process casa-framework MCP server
#   (no URL) which the mock-SDK tool-invoke hook can't simulate. Real chain
#   coverage lives in J.5 manual smoke (live SDK + Anthropic key on production).

set -euo pipefail
export BOOT_TIMEOUT="${BOOT_TIMEOUT:-180}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"
# The worktree-aware resolver now lives in common.sh as e2e_python (#270), so
# every host-side driver picks the same venv-first, aiohttp-verified
# interpreter. E2E_PYTHON remains the operator override, honoured inside
# resolve_python.sh.
E2E_PYTHON="$(e2e_python)"
MOCK_HA_PORT="${MOCK_HA_PORT:-8200}"
MOCK_HA_PID=""
NAME="casa-ha-deleg-$$"

cleanup_all() {
    [ -n "$MOCK_HA_PID" ] && kill "$MOCK_HA_PID" 2>/dev/null || true
    docker stop "$NAME" >/dev/null 2>&1 || true
}
trap cleanup_all EXIT

build_image

# ============================================================
# H-0: boot mock HA MCP + Casa addon
# ============================================================
log "H-0: start mock HA MCP + Casa addon"
MOCK_HA_MALFORMED_TOOL=1 "$E2E_PYTHON" \
    "$REPO_ROOT/test-local/e2e/mock_ha_mcp/server.py" --port "$MOCK_HA_PORT" \
    >/tmp/mock_ha_mcp.log 2>&1 &
MOCK_HA_PID=$!
for _ in $(seq 1 10); do
    curl -sf --max-time 2 "http://localhost:${MOCK_HA_PORT}/_calls" \
        >/dev/null 2>&1 && break
    sleep 0.5
done
curl -sf --max-time 2 "http://localhost:${MOCK_HA_PORT}/_calls" >/dev/null \
    || fail "H-0: mock HA MCP not responding on port $MOCK_HA_PORT"

MSYS_NO_PATHCONV=1 docker run -d --rm --name "$NAME" \
    -p "${HOST_PORT}:8080" \
    -e SUPERVISOR_TOKEN=test-token-v0151 \
    -e CASA_HA_MCP_URL="http://host.docker.internal:${MOCK_HA_PORT}/" \
    --add-host=host.docker.internal:host-gateway \
    "$IMAGE" >/dev/null
wait_healthy "$NAME"
pass "H-0: mock HA MCP + addon up"

# ============================================================
# H-1: addon registered the mock HA URL (CASA_HA_MCP_URL flowed through)
# ============================================================
log "H-1: addon log mentions mock HA URL"
assert_log_contains "$NAME" \
    "Registered Home Assistant MCP server (url=http://host.docker.internal:${MOCK_HA_PORT}/)"
assert_log_contains "$NAME" \
    "Skipping Home Assistant tool MalformedSchema: invalid input schema"
pass "H-1: CASA_HA_MCP_URL override threaded to register_http"

# ============================================================
# H-2: resident facade → direct actions/state → mock HA call history
# ============================================================
log "H-2: eager facade actions/state stay within discovery + loop bounds"
curl -sf --max-time 2 -X POST "http://localhost:${MOCK_HA_PORT}/_reset" \
    >/dev/null

if ! out=$(MSYS_NO_PATHCONV=1 docker exec -i \
        -e PYTHONPATH=/opt/casa \
        "$NAME" /opt/casa/venv/bin/python - \
        "http://host.docker.internal:$MOCK_HA_PORT/" <<'PY'
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager

from ha_mcp_facade import HomeAssistantFacade
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(url: str) -> None:
    history: list[dict] = []

    class TrackingSession:
        def __init__(self, session: ClientSession) -> None:
            self.session = session

        async def initialize(self):
            history.append({"method": "initialize"})
            return await self.session.initialize()

        async def list_tools(self):
            result = await self.session.list_tools()
            history.append({
                "method": "tools/list",
                "tools": [candidate.name for candidate in result.tools],
            })
            return result

        async def send_request(self, request, result_type):
            if request.root.method == "tools/call":
                params = request.root.params
                event = {
                    "method": "tools/call",
                    "name": params.name,
                    "arguments": dict(params.arguments or {}),
                }
                history.append(event)
                try:
                    return await self.session.send_request(request, result_type)
                except Exception as exc:
                    event["error_type"] = type(exc).__name__
                    event["error"] = str(exc)
                    raise

            result = await self.session.send_request(request, result_type)
            history.append({
                "method": "tools/list",
                "tools": [candidate.name for candidate in result.tools],
            })
            return result

        async def call_tool(self, name: str, arguments: dict):
            event = {
                "method": "tools/call",
                "name": name,
                "arguments": dict(arguments),
            }
            history.append(event)
            try:
                return await self.session.call_tool(name, arguments)
            except Exception as exc:
                event["error_type"] = type(exc).__name__
                event["error"] = str(exc)
                raise

    @asynccontextmanager
    async def session_factory():
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                yield TrackingSession(session)

    facade = HomeAssistantFacade(url, {}, session_factory=session_factory)
    await facade.start()
    try:
        assert history[0] == {"method": "initialize"}
        assert history[1]["method"] == "tools/list"
        assert "MalformedSchema" in history[1]["tools"]

        tools = {candidate.name: candidate for candidate in facade.tools}
        assert {"HassTurnOn", "HassTurnOff", "GetLiveContext"} <= set(tools)
        assert "MalformedSchema" not in tools
        resident_connected = len(history)

        turns = [
            ("HassTurnOff", {"name": "office light"}),
            ("HassTurnOn", {"name": "office light"}),
            ("GetLiveContext", {"domain": "lights"}),
        ]
        deltas = []
        for name, arguments in turns:
            before = len(history)
            result = await tools[name].handler(arguments)
            delta = history[before:]
            if len(delta) > 1:
                raise AssertionError(
                    f"guard bound exceeded for {name}: {delta!r}",
                )
            deltas.append(delta)
            assert result.get("is_error") is not True, (result, delta)

        assert deltas[0] == [{
            "method": "tools/call",
            "name": "HassTurnOff",
            "arguments": {"name": "office light"},
        }]
        assert deltas[1] == [{
            "method": "tools/call",
            "name": "HassTurnOn",
            "arguments": {"name": "office light"},
        }]
        assert deltas[2] == [{
            "method": "tools/call",
            "name": "GetLiveContext", "arguments": {},
        }]
        assert not any(
            event["method"] == "tools/list"
            for event in history[resident_connected:]
        ), "tools/list after resident connect"
        print(json.dumps({"status": "OK", "history": history}))
    finally:
        await facade.aclose()


asyncio.run(main(sys.argv[1]))
PY
); then
    printf '%s\n' "$out" | tail -30 >&2
    fail "H-2: eager facade probe exited non-zero"
fi
printf '%s\n' "$out" | tail -1 | grep -qF '"status": "OK"' \
    || { printf '%s\n' "$out" | tail -10 >&2; fail "H-2: facade probe did not report OK"; }

curl -sf --max-time 2 "http://localhost:${MOCK_HA_PORT}/_calls" \
    | "$E2E_PYTHON" -c '
import json, sys
actual = json.load(sys.stdin)
expected = [
    {"name": "HassTurnOff", "arguments": {"name": "office light"}},
    {"name": "HassTurnOn", "arguments": {"name": "office light"}},
    {"name": "GetLiveContext", "arguments": {}},
]
assert actual == expected, (actual, expected)
'
pass "H-2: direct off/on + normalized state query used one call each"

# ============================================================
# H-3: raw fallback agent_loader → SDK options → mock HA
# ============================================================
log "H-3: raw fallback still resolves butler grant + reaches mock HA"
curl -sf --max-time 2 -X POST "http://localhost:${MOCK_HA_PORT}/_reset" \
    >/dev/null

MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c \
    "echo '"'[{"server":"homeassistant","tool":"HassTurnOn","args":{"name":"bedroom"}}]'"' > /data/mock_sdk_tool_invoke.json"

# NOTE: do NOT prefix MSYS_NO_PATHCONV here — the host path starts with /c/
# and needs Git Bash's automatic translation to C:\. The container-side path
# `casa-...:/tmp/...` doesn't start with / so it isn't translated.
docker cp \
    "$REPO_ROOT/test-local/e2e/harnesses/ha_delegation_butler.py" \
    "$NAME:/tmp/ha_delegation_butler.py" >/dev/null

if ! out=$(MSYS_NO_PATHCONV=1 docker exec \
        -e VOICE_AGENT_MODEL=haiku \
        "$NAME" /opt/casa/venv/bin/python /tmp/ha_delegation_butler.py 2>&1); then
    printf '%s\n' "$out" | tail -30 >&2
    fail "H-3: butler-resident harness exited non-zero"
fi
printf '%s\n' "$out" | tail -1 | grep -qF OK \
    || { printf '%s\n' "$out" | tail -10 >&2; fail "H-3: harness did not print OK"; }

calls=$(curl -sf --max-time 2 "http://localhost:${MOCK_HA_PORT}/_calls" \
    | "$E2E_PYTHON" -c "import sys, json; print(len(json.load(sys.stdin)))")
[ "$calls" -ge 1 ] \
    || fail "H-3: raw agent_loader→SDK chain did not reach mock HA (calls=$calls)"
pass "H-3: raw fallback registry → SDK options → mock HA works ($calls call(s))"

stop_container "$NAME"
log "All H-* checkpoints green."
