#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

# Repo root is two levels up from test-local/e2e/
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# Reuse the Windows-aware tmp base from test_migration.sh. Borrowing via
# copy rather than source-sharing keeps this script stand-alone.
_tmpbase() {
    local wintemp
    wintemp="$(powershell.exe -NoProfile -Command "[System.IO.Path]::GetTempPath()" 2>/dev/null | tr -d '\r\n')"
    if [ -n "$wintemp" ]; then
        local drive="${wintemp:0:1}"
        local rest="${wintemp:2}"
        rest="$(printf '%s' "$rest" | tr '\\' '/')"
        printf '/%s%s' "$(printf '%s' "$drive" | tr '[:upper:]' '[:lower:]')" "$rest"
    else
        printf '/tmp'
    fi
}
TMPBASE="$(_tmpbase)"
# The Windows-derived base is only reachable from Git Bash; under WSL the
# /c/... form does not exist (and CI has no powershell.exe at all). Fall
# back to /tmp whenever the computed base is not actually usable.
if ! mkdir -p "$TMPBASE" 2>/dev/null || [ ! -w "$TMPBASE" ]; then
    TMPBASE=/tmp
fi

cleanup_all() {
    docker ps -q --filter "name=casa-deleg-.*-$$" | xargs -r docker stop >/dev/null 2>&1 || true
}
trap cleanup_all EXIT

build_image

# ============================================================
# D-1: zero-specialists baseline (v0.100.0 cutover contract)
# ============================================================
# The image ships NO bundled specialists — every specialist is installed
# from its component repository (see test_specialist_install_from_repo.sh
# for the install path). A fresh boot must come up healthy with an empty
# specialist set and must NOT seed any specialist directory into /config.
log "D-1: zero-specialists baseline"

NAME="casa-deleg-d1-$$"
start_container "$NAME" >/dev/null
wait_healthy "$NAME"

# No specialist dir seeded to user config (the old bundled-finance seeding
# is gone with the cutover). MSYS_NO_PATHCONV=1 prevents Git Bash from
# rewriting the container-side path to a Windows one.
MSYS_NO_PATHCONV=1 docker exec "$NAME" test ! -e /config/agents/specialists/finance \
    && MSYS_NO_PATHCONV=1 docker exec "$NAME" test -d /config/agents/specialists \
    || fail "D-1: expected an empty /config/agents/specialists (no bundled finance)"

# Summary line: nothing enabled, disabled, or failed.
# Use assert_log_contains (polls up to 15s, grep -qF) — covers both the
# ERE \[\] portability gap and the docker-logs stdout lag on CI.
assert_log_contains "$NAME" "Specialists: enabled=[] disabled=[] failed=[]"

stop_container "$NAME"
pass "D-1 zero-specialists baseline"

# ============================================================
# D-2: orphan legacy specialist dir fails per-slug, boot survives
# ============================================================
# A pre-cutover /config carrying a legacy 4-file specialist dir (exactly
# what a production box upgraded from a bundled-finance release has, until
# the specialist is reinstalled from its repository) must NOT crash-loop
# boot: the slug fails to load with a per-slug error, siblings and
# residents continue, and the user's directory is left byte-identical.
log "D-2: orphan legacy specialist dir"

NAME="casa-deleg-d2-$$"
TMP_D2="${TMPBASE}/casa-deleg-d2-$$"
mkdir -p "$TMP_D2"
cp -r "${REPO_ROOT}/test-local/fixtures/delegation-enabled/." "$TMP_D2/"

# Capture fixture directory hash (sha256 over every file's content) to verify
# no boot-time side-effect mutated the user's YAMLs. Finds under a subshell
# so the cwd change doesn't leak.
_dir_hash() {
    ( cd "$1" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}' )
}
FIXTURE_HASH_BEFORE=$(_dir_hash "$TMP_D2/agents/specialists/finance")

MSYS_NO_PATHCONV=1 docker run -d --rm --name "$NAME" \
    -p "${HOST_PORT}:8080" \
    -v "${TMP_D2}:/config" \
    "$IMAGE" >/dev/null
wait_healthy "$NAME"

# Per-slug failure surfaced loudly (no role artifact for an uninstalled
# specialist), without poisoning the boot.
assert_log_contains "$NAME" "Specialist 'finance' failed to load:"

# Summary line reflects the failed slug — nothing enabled or disabled.
assert_log_contains "$NAME" "Specialists: enabled=[] disabled=[] failed=['finance']"

stop_container "$NAME"

# Post-boot: the user's directory must still be content-identical to its
# pre-boot state (no migration rewrote or pruned the orphan dir).
FIXTURE_HASH_AFTER=$(_dir_hash "$TMP_D2/agents/specialists/finance")
[ "$FIXTURE_HASH_BEFORE" = "$FIXTURE_HASH_AFTER" ] \
    || fail "D-2: fixture finance/ dir was modified by boot (hash drift)"

# Cleanup tmp (root-owned workspace/ files need containerized rm).
docker run --rm -v "${TMP_D2}:/target" --entrypoint sh "$IMAGE" \
    -c 'rm -rf /target/workspace /target/data' >/dev/null 2>&1 || true
rm -rf "$TMP_D2" 2>/dev/null || true

pass "D-2 orphan legacy specialist dir"
