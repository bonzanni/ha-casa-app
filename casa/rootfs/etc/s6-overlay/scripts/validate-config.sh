#!/command/with-contenv /opt/casa/scripts/core-bashio.sh
# 5.5 item 3: strip ANSI from bashio output for clean docker logs.
export BASHIO_LOG_NO_COLORS=true
export NO_COLOR=1

if ! bashio::config.has_value 'claude_oauth_token'; then
    bashio::exit.nok "claude_oauth_token is required. Run 'claude setup-token' on your local machine."
fi

bashio::log.info "Configuration validated."
