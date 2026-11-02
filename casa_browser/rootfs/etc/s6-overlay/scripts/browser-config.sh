#!/command/with-contenv bash
# Root bootstrap for Casa Browser (s6 oneshot init-browser-config), before any service.
# Reads /data/options.json (root only), writes the broker's private config and the
# controller's public options and effective locale, reads Home Assistant's language once
# through the Core API proxy, and removes SUPERVISOR_TOKEN from the s6 container
# environment so no service started afterwards can inherit it. See
# /opt/casa_browser/options.py (bootstrap).
set -euo pipefail
export PYTHONPATH=/opt
exec /opt/venv/bin/python -m casa_browser.options
