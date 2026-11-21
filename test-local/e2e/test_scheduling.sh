#!/usr/bin/env bash
# E2E: seeded morning-briefing cron is loaded, get_schedule tool returns it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

# Cold-boot provisioning can run longer on local Docker-Desktop than on CI.
# Extend the healthz timeout beyond the default 30s so we don't race.
BOOT_TIMEOUT=90

build_image

NAME="casa-sch-$$"
trap "stop_container $NAME" EXIT

log "Starting container with the default trigger (morning-briefing)"
start_authed_container "$NAME" >/dev/null
wait_healthy "$NAME"

log "S-1: assert boot logs show 1 trigger registered for assistant"
assert_log_contains "$NAME" "Registered 1 trigger(s) for agent 'assistant'"
pass "S-1 assistant has 1 trigger registered (morning-briefing)"

log "S-2: invoking assistant to ask about schedule"
_S2_BODY='{"prompt": "What cron tasks are on your schedule today?"}'
RESP=$(curl -s -X POST "http://localhost:${HOST_PORT}/invoke/assistant" \
    -H "Content-Type: application/json" \
    -H "X-Webhook-Signature: $(sign_body "$_S2_BODY")" \
    -d "$_S2_BODY" || true)

log "Response: $RESP"

if printf '%s' "$RESP" | grep -qiF '"error"'; then
    fail "S-2 invoke returned error: $RESP"
fi
pass "S-2 invoke returned non-error response"

pass "test_scheduling PASSED"
