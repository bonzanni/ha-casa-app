#!/command/with-contenv /opt/casa/scripts/core-bashio.sh
# init-secrets-backend: decide where this install keeps its secrets, before
# anything reads one.
#
# With a 1Password service-account token configured, secrets are in 1Password,
# as they always were. With none, they are in the store built into Casa. The
# choice is one word in a file, `1password` or `builtin`, and that file is the
# only thing that says which: /usr/local/bin/op and the start scripts
# (secrets-backend-lib.sh) read it, and no environment variable overrides it.
# A token that is set but does not work is still 1Password; nothing falls back
# to the built-in store.
#
# The file is written under another name and renamed, so a reader never sees
# it half written.
export BASHIO_LOG_NO_COLORS=true
export NO_COLOR=1

_token="$(bashio::config 'onepassword_service_account_token')"
if [ -z "$_token" ] || [ "$_token" = "null" ]; then
    _backend=builtin
else
    _backend=1password
fi
unset _token

mkdir -p /run/casa-vault
chmod 0755 /run/casa-vault
printf '%s\n' "$_backend" > /run/casa-vault/backend.new
mv -f /run/casa-vault/backend.new /run/casa-vault/backend

bashio::log.info "Secrets backend: ${_backend}"
