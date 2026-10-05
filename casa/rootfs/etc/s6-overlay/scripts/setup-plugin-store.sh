#!/command/with-contenv /opt/casa/scripts/core-bashio.sh
# ==============================================================================
# init-plugin-store: bundled-artifact import + registry seed +
# plugin-health report. Runs AFTER init-setup-configs (config_sync)
# and BEFORE svc-casa / svc-casa-mcp (s6 dependencies enforce this — spec 3.6).
# ALWAYS exits 0: failures become /data/plugin-health.json issues; a nonzero
# exit here would block svc-casa.
# ==============================================================================
export HOME=/config/cc-home
/opt/casa/venv/bin/python3 /opt/casa/plugin_boot.py \
  || bashio::log.warning "plugin store boot degraded (non-fatal; see /data/plugin-health.json)"
exit 0
