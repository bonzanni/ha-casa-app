#!/command/with-contenv /opt/casa/scripts/core-bashio.sh
# init-memory-maintenance: carry out a pending memory wipe before any service
# that can read or write memory starts (svc-vault depends on this oneshot
# directly; every other longrun depends on init-setup-configs, which depends
# on it).
#
# It always exits 0, so a failure never stops the container from starting.
# The failure is recorded in /data/memory/maintenance.json, and casa-main then
# starts in notification-only mode.
export BASHIO_LOG_NO_COLORS=true
export NO_COLOR=1

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/casa /opt/casa/venv/bin/python3 -m memory.maintenance \
    || bashio::log.error "memory maintenance exited non-zero (recorded in its intent when it got that far)"
exit 0
