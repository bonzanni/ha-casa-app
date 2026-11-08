#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

build_image

NAME="casa-invoke-$$"
trap "stop_container $NAME" EXIT

log "Starting container $NAME"
start_authed_container "$NAME" >/dev/null
wait_healthy "$NAME"

log "C-3a: two /invoke calls with caller-supplied distinct chat_ids"
signed_invoke "$HOST_PORT" assistant '{"prompt":"hi A","context":{"chat_id":"user-A"}}' >/dev/null \
    || fail "first /invoke failed"
signed_invoke "$HOST_PORT" assistant '{"prompt":"hi B","context":{"chat_id":"user-B"}}' >/dev/null \
    || fail "second /invoke failed"

# Give the session-registry write a beat to flush.
sleep 1

log "Inspecting /data/sessions.json"
keys=$(docker exec "$NAME" sh -c \
    'python3 -c "import json,sys; print(\",\".join(sorted(json.load(open(\"/data/sessions.json\")).keys())))"')
echo "  registry keys: $keys"
# v0.80.0 (spec A2): session identity is now the collision-safe, role-scoped
# channel-first v2 key (webhook-v2-<sha256(channel\0role\0scope)[:24]>), not
# the legacy webhook-<scope>. Compute the expected keys the same way the code
# does (invoke uses the target agent's role — here `assistant`) so this test
# tracks the real format instead of hardcoding it.
key_for() {  # key_for <scope>
    docker exec "$NAME" sh -c \
        "python3 -c \"import sys; sys.path.insert(0,'/opt/casa'); from session_registry import build_scoped_session_key as k; print(k('webhook','assistant','$1'))\""
}
exp_a=$(key_for user-A); exp_b=$(key_for user-B); exp_default=$(key_for default)
echo "  expected: A=$exp_a B=$exp_b (default MUST be absent: $exp_default)"
echo "$keys" | grep -q "$exp_a" \
    || fail "missing v2 session entry for chat_id user-A ($exp_a)"
echo "$keys" | grep -q "$exp_b" \
    || fail "missing v2 session entry for chat_id user-B ($exp_b)"
echo "$keys" | grep -q "$exp_default" \
    && fail "unwanted default-scoped entry present (BUG-I1 regression)"
pass "C-3a distinct caller-supplied chat_ids"

log "C-3b: two /invoke calls WITHOUT chat_id get unique UUIDs"
signed_invoke "$HOST_PORT" assistant '{"prompt":"hi X"}' >/dev/null
signed_invoke "$HOST_PORT" assistant '{"prompt":"hi Y"}' >/dev/null

sleep 1
webhook_keys=$(docker exec "$NAME" sh -c \
    'python3 -c "import json; d=json.load(open(\"/data/sessions.json\")); print(chr(10).join(k for k in d if k.startswith(\"webhook-\")))"')
# The two chat_id-less invokes each get a distinct UUID scope, so their v2
# keys differ from the user-A/user-B keys computed above. Count webhook
# sessions that are neither of those two → expect exactly 2.
uuid_count=$(echo "$webhook_keys" | grep -Ev "^($exp_a|$exp_b)$" | grep -c "^webhook-" || true)
[ "$uuid_count" -eq 2 ] \
    || fail "expected 2 UUID-backed invoke sessions, got $uuid_count (keys: $webhook_keys)"
pass "C-3b each chat_id-less invoke gets its own session"

log "C-4: the plugin store boots empty and clean (no bundled plugins since v0.349.0)"
# The init-plugin-store oneshot still runs on every boot: it creates the store,
# runs the registry/resolve pass and writes /data/plugin-health.json. The image
# carries no bundle and no default registry, so a fresh install has no plugins
# and nothing to report.
store_count=$(docker exec "$NAME" sh -c 'test -d /config/plugins/store && ls -1A /config/plugins/store | wc -l') \
    || fail "init-plugin-store did not create /config/plugins/store"
[ "$store_count" -eq 0 ] \
    || fail "expected an empty /config/plugins/store on a fresh boot, got $store_count entries"
reg_count=$(docker exec "$NAME" sh -c \
    'if [ -e /config/plugins/registry.json ]; then jq ".plugins // [] | length" /config/plugins/registry.json; else echo 0; fi')
[ "$reg_count" -eq 0 ] \
    || fail "expected no registry entries on a fresh boot, got $reg_count"
issue_count=$(docker exec "$NAME" sh -c 'jq ".issues | length" /data/plugin-health.json') \
    || fail "init-plugin-store wrote no readable /data/plugin-health.json"
[ "$issue_count" -eq 0 ] \
    || fail "expected no plugin-health issues on a fresh boot, got $issue_count: $(docker exec "$NAME" cat /data/plugin-health.json)"
pass "C-4 plugin store boots empty and clean: store=$store_count registry=$reg_count issues=$issue_count"
