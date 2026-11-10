#!/usr/bin/env bash
# Manual smoke test: exercises the REAL Telegram Bot API calls the agent log
# makes against the configured test supergroup. Requires:
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
MSG_ID=$(curl -s -X POST "${API}/sendMessage" \
    -d chat_id="${TELEGRAM_TEST_SUPERGROUP_ID}" \
    -d message_thread_id="${TOPIC_ID}" \
    -d disable_notification=true \
    -d text="Smoke log line" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["result"]["message_id"])')
[ -n "$MSG_ID" ] || fail "sendMessage in topic returned no message_id"

# --- 4. editMessageText — the log's rolling message ---------------------
log "4. editMessageText (rolling log message)"
EDIT_OK=$(curl -s -X POST "${API}/editMessageText" \
    -d chat_id="${TELEGRAM_TEST_SUPERGROUP_ID}" \
    -d message_id="${MSG_ID}" \
    -d text="Smoke log line
second line" \
    | python3 -c 'import sys, json; d=json.load(sys.stdin); print(d.get("ok"))')
[ "$EDIT_OK" = "True" ] || fail "editMessageText failed"

# --- 5. deleteMessages — the log's retention ----------------------------
log "5. deleteMessages (retention)"
DEL_OK=$(curl -s -X POST "${API}/deleteMessages" \
    -d chat_id="${TELEGRAM_TEST_SUPERGROUP_ID}" \
    --data-urlencode "message_ids=[${MSG_ID}]" \
    | python3 -c 'import sys, json; d=json.load(sys.stdin); print(d.get("ok"))')
[ "$DEL_OK" = "True" ] || fail "deleteMessages failed"

# --- 6. deleteForumTopic (cleanup) --------------------------------------
log "6. cleanup — deleteForumTopic"
curl -s -X POST "${API}/deleteForumTopic" \
    -d chat_id="${TELEGRAM_TEST_SUPERGROUP_ID}" \
    -d message_thread_id="${TOPIC_ID}" \
    > /dev/null

echo "=== Telegram smoke passed ==="
