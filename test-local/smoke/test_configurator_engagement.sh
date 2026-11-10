#!/usr/bin/env bash
# Manual smoke for the configurator against the live Telegram Bot API.
# Requires .env.smoke with TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and
# optionally TELEGRAM_ENGAGEMENT_SUPERGROUP_ID (the log group).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

if [ -f .env.smoke ]; then
    set -a; . ./.env.smoke; set +a
fi

: "${TELEGRAM_BOT_TOKEN:?set in .env.smoke}"
: "${TELEGRAM_CHAT_ID:?set in .env.smoke}"
: "${TELEGRAM_ENGAGEMENT_SUPERGROUP_ID:=0}"

API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

echo "=== Configurator smoke test (v0.12.0) ==="
echo "Bot:        $(curl -s "$API/getMe" | jq -r .result.username)"
echo "User chat:  $TELEGRAM_CHAT_ID"
echo "Log group:  $TELEGRAM_ENGAGEMENT_SUPERGROUP_ID (0 = none)"
echo ""

echo "Step 1: Send config request to Ellen (1:1 chat)"
echo "  'Please add a hourly_check trigger to assistant that fires every hour'"
read -p "    Press enter after you send the message in Telegram... "

echo "Step 2: Watch the configurator's run"
echo "  With the log group set up: its steps and words appear in the"
echo "  configurator's log topic there. Without it: a step line in the 1:1 chat."
read -p "    Press enter after you see the run's steps... "

echo "Step 3: When the configurator asks to confirm, Ellen puts the question"
echo "  to you in the 1:1 chat. Reply 'yes do it' there."
read -p "    Press enter after you reply... "

echo "Step 4: Wait for the run to finish + casa_reload_triggers (~5-10s)"
read -p "    Press enter after Ellen tells you it is done... "

echo "Step 5: Verify trigger landed (SSH to the HA host):"
echo "  ssh <ha-host> sudo -n docker exec addon_91d4d4c8_casa \\"
echo "    cat /config/agents/assistant/triggers.yaml"
read -p "    Press enter after manual verification... "

echo "Step 6: Verify Ellen narrated the outcome in 1:1 chat"
read -p "    Press enter after confirming narration... "

echo "Step 7: Verify no addon restart (soft reload only):"
echo "  ssh <ha-host> sudo -n docker exec hassio_cli \\"
echo "    ha apps info 91d4d4c8_casa --raw-json | grep -E '^(version|state)'"
echo "  Uptime should be unchanged."
read -p "    Press enter after manual verification... "

echo "=== PASS ==="
echo ""
echo "Cleanup: ask Ellen to remove hourly_check trigger from assistant."
echo "Verify triggers.yaml back to pre-test state."
