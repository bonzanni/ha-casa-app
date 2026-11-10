#!/bin/sh
# Test override: exports env vars from options.json for the casa service.
# In HA, the service run script uses bashio::config. Here we write to
# /var/run/s6/container_environment/ which s6 reads automatically.
OPTIONS=/data/options.json
S6_ENV="/var/run/s6/container_environment"

mkdir -p "$S6_ENV"

# Parity with the real svc-casa/run: the add-on option is the ONLY source of a
# WEBHOOK_SECRET override. casa_core treats a non-empty WEBHOOK_SECRET as
# authoritative over /data/webhook_secret, so a value inherited from the
# container environment (a stray `docker run -e`) would outrank the file that
# every signer actually uses, and nothing would verify. Clear it first, then
# let the loop below re-export it only when the option really sets it.
rm -f "${S6_ENV}/WEBHOOK_SECRET"

# Export each option as an s6 container env var.
# v0.125.0 (#228) removed telegram_delivery_mode, honcho_api_url and
# honcho_api_key from config.yaml's schema, so exporting them was dead
# weight (#262).
for key in public_url telegram_bot_token telegram_chat_id telegram_transport \
           webhook_secret enable_terminal \
           primary_agent_model voice_agent_model; do
    val=$(jq -r ".${key} // empty" "$OPTIONS")
    upper_key=$(echo "$key" | tr '[:lower:]' '[:upper:]')
    if [ -n "$val" ]; then
        printf '%s' "$val" > "${S6_ENV}/${upper_key}"
    fi
done

# Special case: the Claude Code CLI expects CLAUDE_CODE_OAUTH_TOKEN
# (not CLAUDE_OAUTH_TOKEN). The real HA run script maps this explicitly.
val=$(jq -r '.claude_oauth_token // empty' "$OPTIONS")
if [ -n "$val" ]; then
    printf '%s' "$val" > "${S6_ENV}/CLAUDE_CODE_OAUTH_TOKEN"
fi

# scope_threshold -> CASA_SCOPE_THRESHOLD was removed here in #262: the option
# is gone from config.yaml's schema and the real svc-casa/run no longer
# exports it either.

# The agent log's forum group (0 = no logs; always export so consumers can check).
val=$(jq -r '.telegram_engagement_supergroup_id // 0' "$OPTIONS")
printf '%s' "$val" > "${S6_ENV}/TELEGRAM_ENGAGEMENT_SUPERGROUP_ID"

# Static version for test mode
printf 'dev' > "${S6_ENV}/CASA_VERSION"

TELEGRAM_BOT_API_BASE="$(jq -r '.telegram_bot_api_base // ""' /data/options.json)"
export TELEGRAM_BOT_API_BASE
if [ -n "$TELEGRAM_BOT_API_BASE" ]; then
  echo "$TELEGRAM_BOT_API_BASE" > /var/run/s6/container_environment/TELEGRAM_BOT_API_BASE
fi

echo "[INFO] Environment exported from options.json (local test mode)."
