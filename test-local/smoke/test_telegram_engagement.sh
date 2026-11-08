#!/usr/bin/env bash
# Manual smoke test: exercises the REAL Telegram Bot API against the
# configured test supergroup. Requires:
#   TELEGRAM_BOT_TOKEN            — same bot token Casa uses
#   TELEGRAM_TEST_SUPERGROUP_ID   — a dedicated test forum supergroup
#
# Values may be exported in the environment OR stored in
# test-local/smoke/.env.smoke (gitignored). The .env.smoke file is
# auto-sourced if present; see .env.smoke.example for the template.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "${HERE}/.env.smoke" ]; then
    # shellcheck source=/dev/null
    set -a; . "${HERE}/.env.smoke"; set +a
fi

: "${TELEGRAM_BOT_TOKEN:?set TELEGRAM_BOT_TOKEN (env or test-local/smoke/.env.smoke)}"
: "${TELEGRAM_TEST_SUPERGROUP_ID:?set TELEGRAM_TEST_SUPERGROUP_ID (env or test-local/smoke/.env.smoke)}"

API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

log()  { printf '\n==> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# --- 1. Permissions check ----------------------------------------------
log "1. getMe + getChatMember"
ME=$(curl -s "${API}/getMe" | python3 -c 'import sys, json; print(json.load(sys.stdin)["result"]["id"])')
MEMBER=$(curl -s "${API}/getChatMember?chat_id=${TELEGRAM_TEST_SUPERGROUP_ID}&user_id=${ME}" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d.get("ok"), d
print(d["result"].get("can_manage_topics", False))
')
[ "$MEMBER" = "True" ] || fail "bot lacks can_manage_topics in test supergroup"

# --- 2. createForumTopic ------------------------------------------------
log "2. createForumTopic"
TOPIC_JSON=$(curl -s -X POST "${API}/createForumTopic" \
    -d chat_id="${TELEGRAM_TEST_SUPERGROUP_ID}" \
    -d name="Smoke #$(date +%s)" \
    -d icon_color=7322096)
TOPIC_ID=$(echo "$TOPIC_JSON" | python3 -c 'import sys, json; print(json.load(sys.stdin)["result"]["message_thread_id"])')
[ -n "$TOPIC_ID" ] || fail "createForumTopic returned no message_thread_id"

# --- 3. sendMessage into topic ------------------------------------------
log "3. sendMessage with message_thread_id"
SEND_OK=$(curl -s -X POST "${API}/sendMessage" \
    -d chat_id="${TELEGRAM_TEST_SUPERGROUP_ID}" \
    -d message_thread_id="${TOPIC_ID}" \
    -d text="Smoke test message" \
    | python3 -c 'import sys, json; d=json.load(sys.stdin); print(d.get("ok"))')
[ "$SEND_OK" = "True" ] || fail "sendMessage in topic failed"

# --- 4. editForumTopic — flip icon to ✅ --------------------------------
log "4. editForumTopic (✅ icon)"
EDIT_OK=$(curl -s -X POST "${API}/editForumTopic" \
    -d chat_id="${TELEGRAM_TEST_SUPERGROUP_ID}" \
    -d message_thread_id="${TOPIC_ID}" \
    -d name="Smoke #$(date +%s) ✅" \
    | python3 -c 'import sys, json; d=json.load(sys.stdin); print(d.get("ok"))')
[ "$EDIT_OK" = "True" ] || fail "editForumTopic failed"

# --- 5. closeForumTopic -------------------------------------------------
log "5. closeForumTopic"
CLOSE_OK=$(curl -s -X POST "${API}/closeForumTopic" \
    -d chat_id="${TELEGRAM_TEST_SUPERGROUP_ID}" \
    -d message_thread_id="${TOPIC_ID}" \
    | python3 -c 'import sys, json; d=json.load(sys.stdin); print(d.get("ok"))')
[ "$CLOSE_OK" = "True" ] || fail "closeForumTopic failed"

# --- 6. setMyCommands + getMyCommands -----------------------------------
log "6. setMyCommands + getMyCommands (supergroup scope)"
SCOPE_JSON="$(python3 -c 'import json; print(json.dumps({"type":"chat","chat_id":'${TELEGRAM_TEST_SUPERGROUP_ID}'}))')"
CMDS_JSON='[{"command":"cancel","description":"Cancel this engagement"},{"command":"complete","description":"Mark complete"}]'
curl -s -X POST "${API}/setMyCommands" \
    --data-urlencode "scope=${SCOPE_JSON}" \
    --data-urlencode "commands=${CMDS_JSON}" \
    > /dev/null
GOT=$(curl -s -X POST "${API}/getMyCommands" \
    --data-urlencode "scope=${SCOPE_JSON}" \
    | python3 -c 'import sys, json; print(len(json.load(sys.stdin)["result"]))')
[ "$GOT" = "2" ] || fail "setMyCommands/getMyCommands roundtrip got $GOT commands"

# --- 7. deleteForumTopic (cleanup) --------------------------------------
log "7. cleanup — deleteForumTopic"
curl -s -X POST "${API}/deleteForumTopic" \
    -d chat_id="${TELEGRAM_TEST_SUPERGROUP_ID}" \
    -d message_thread_id="${TOPIC_ID}" \
    > /dev/null

echo "=== Telegram smoke passed ==="
