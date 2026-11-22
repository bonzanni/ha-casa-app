#!/bin/sh
# What the two 1Password options mean under the secrets backend this install
# uses. Sourced by the start scripts that read those options.
#
# The backend is chosen once, when the container starts: secrets-backend.sh
# writes `builtin` to a file when no 1Password service-account token is
# configured, `1password` when one is. These functions read that file and
# nothing else; no environment variable selects the backend. On 1Password
# they return the option values as they are (bashio's "null" for an unset
# option becomes empty), so there the scripts pass on exactly what is
# configured.
#
# Shell builtins only, in the POSIX dialect: nothing is looked up on PATH, and
# the file can be sourced by bash and by sh.

# Prints `builtin` or `1password`. Only the exact word `builtin` on the first
# line means the built-in store; a missing or unreadable file, or any other
# content, means 1Password. /usr/local/bin/op reads the file the same way.
casa_secrets_backend() {
    _casa_backend=
    if [ -r /run/casa-vault/backend ]; then
        IFS= read -r _casa_backend < /run/casa-vault/backend || :
    fi
    if [ "$_casa_backend" = builtin ]; then
        printf '%s\n' builtin
    else
        printf '%s\n' 1password
    fi
}

# casa_op_token VALUE: the OP_SERVICE_ACCOUNT_TOKEN to run with, VALUE being
# the onepassword_service_account_token option. The built-in store takes no
# token; what requires one to be set gets a fixed word that is no secret.
casa_op_token() {
    if [ "$(casa_secrets_backend)" = builtin ]; then
        printf '%s\n' casa-builtin-store
    elif [ "${1-}" != null ]; then
        printf '%s\n' "${1-}"
    fi
}

# casa_op_vault VALUE: the vault name to use, VALUE being the
# onepassword_default_vault option. The built-in store has one vault, named
# Casa unless the option names it otherwise.
casa_op_vault() {
    if [ -n "${1-}" ] && [ "${1-}" != null ]; then
        printf '%s\n' "$1"
    elif [ "$(casa_secrets_backend)" = builtin ]; then
        printf '%s\n' Casa
    fi
}
