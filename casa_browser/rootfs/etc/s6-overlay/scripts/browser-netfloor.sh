#!/command/with-contenv bash
# Root init for Casa Browser (s6 oneshot init-browser-netfloor), after init-browser-config
# and before every service: installs the nftables egress floor (one permitted route per
# service user) and, only once it reads back, the marker the browser launcher requires.
# A failure here fails the oneshot, and no service that depends on it starts.
# See /opt/casa_browser/netfloor.py.
set -euo pipefail
export PYTHONPATH=/opt
exec /opt/venv/bin/python -m casa_browser.netfloor install
