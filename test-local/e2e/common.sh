#!/usr/bin/env bash
# Shared helpers for Casa E2E tests. Source this from each test script.
set -euo pipefail

IMAGE="${IMAGE:-casa-test}"
# Randomise the host port per-script run so back-to-back suites don't clash
# on a port Docker has not yet released (1-2s after docker stop).
HOST_PORT="${HOST_PORT:-$((18080 + RANDOM % 1000))}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-30}"

log()  { printf '[e2e] %s\n' "$*" >&2; }
fail() { printf '[e2e FAIL] %s\n' "$*" >&2; exit 1; }
pass() { printf '[e2e PASS] %s\n' "$*" >&2; }

# Milliseconds since the epoch (#271).
#
# NOT `date +%s%3N`: the width modifier is a GNU extension. uutils coreutils
# (the Rust reimplementation, default on some distros and WSL images) silently
# IGNORES it and emits full nanoseconds — 19 digits where GNU gives 13 — so a
# subtraction of two such stamps yields garbage. Timing probes then fail their
# upper bound and blame the product.
#
# The digit check is load-bearing, not defensive noise: on BSD/macOS `date`
# does not support %N at all and emits a literal "N", which makes the
# arithmetic expansion below abort under `set -euo pipefail` BEFORE any caller
# can range-check the result — an opaque shell error instead of a clear one.
now_ms() {
    local ns
    ns="$(date +%s%N)" || fail "harness clock: 'date +%s%N' failed"
    case "$ns" in
        ''|*[!0-9]*)
            fail "harness clock: 'date +%s%N' returned '$ns' — this harness needs a date(1) supporting %N (GNU or uutils coreutils)"
            ;;
    esac
    printf '%s\n' "$(( ns / 1000000 ))"
}

build_image() {
    log "Building $IMAGE from test-local/Dockerfile.test"
    docker build -f test-local/Dockerfile.test -t "$IMAGE" . >/dev/null
}

# --- Release A: /invoke + /webhook are fail-closed (401/403 without a valid
# signature). Tests that exercise those endpoints boot with an auth-ON options
# override and sign each request. WEBHOOK_SECRET_E2E matches options.auth.json.
WEBHOOK_SECRET_E2E="e2e-invoke-secret"
_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# e2e_python — path to a HOST-side Python that can import aiohttp (#270).
#
# Host-side drivers (the voice WS smoke, the mock Telegram server) import
# aiohttp, which on a dev box lives in the project venv, not in system python3.
# resolve_python.sh honours $E2E_PYTHON, else prefers venv_test/, else falls
# back to python3 — and verifies the choice can actually import aiohttp before
# returning it. Every host-side driver with third-party imports must go through
# this.
#
# NOT for `docker exec … python3`: that runs the container's interpreter and
# must stay untouched. Host-side `python3 -c` one-liners that only use stdlib
# `json` also do not need this — any python3 will do.
#
# Assign the result once per script (`PY="$(e2e_python)"`); each call re-runs
# the aiohttp probe. On failure the resolver's diagnostic is already on stderr
# and the non-zero return trips `set -e` at the assignment.
e2e_python() {
    local git_common_dir shared_repo_root
    # Worktree-aware: venv_test/ lives in the shared checkout, not in a linked
    # worktree, so resolve through --git-common-dir rather than $_REPO_ROOT.
    git_common_dir="$(git -C "$_REPO_ROOT" rev-parse --git-common-dir)"
    case "$git_common_dir" in
        /*) ;;
        *) git_common_dir="$_REPO_ROOT/$git_common_dir" ;;
    esac
    shared_repo_root="$(cd "$(dirname "$git_common_dir")" && pwd)"
    bash "$_REPO_ROOT/test-local/e2e/resolve_python.sh" "$shared_repo_root"
}

# start_authed_container <name> [extra docker args...]
# Like start_container but mounts the auth-ON options over /data/options.json.
start_authed_container() {
    local name="$1"; shift
    start_container "$name" \
        -v "${_REPO_ROOT}/test-local/options.auth.json:/data/options.json:ro" \
        "$@"
}

# sign_body <body> -> HMAC-SHA256 hex of the body under WEBHOOK_SECRET_E2E.
sign_body() {
    printf '%s' "$1" \
        | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET_E2E" -r \
        | cut -d' ' -f1
}

# signed_invoke <host_port> <agent> <json-body> -> curl output; fails on non-2xx.
signed_invoke() {
    local port="$1" agent="$2" body="$3"
    curl -sf -X POST "http://localhost:${port}/invoke/${agent}" \
        -H 'Content-Type: application/json' \
        -H "X-Webhook-Signature: $(sign_body "$body")" \
        -d "$body"
}

# start_container <name> [extra docker args...]
# Prints the container id on stdout.
# Maps ${HOST_PORT}:8080 always; if EXT_PORT is set, also maps
# ${EXT_PORT}:18065 for tests that exercise the external server block.
start_container() {
    local name="$1"; shift
    local port_args=(-p "${HOST_PORT}:8080")
    if [ -n "${EXT_PORT:-}" ]; then
        port_args+=(-p "${EXT_PORT}:18065")
    fi
    docker run -d --rm --name "$name" \
        "${port_args[@]}" \
        "$@" "$IMAGE" >/dev/null
    echo "$name"
}

wait_healthy() {
    local name="$1"
    local i
    for i in $(seq 1 "$BOOT_TIMEOUT"); do
        if curl -sf "http://localhost:${HOST_PORT}/healthz" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    docker logs "$name" 2>&1 | tail -30 >&2
    fail "container $name never became healthy within ${BOOT_TIMEOUT}s"
}

stop_container() {
    local name="$1"
    docker stop "$name" >/dev/null 2>&1 || true
}

assert_log_contains() {
    # Poll docker logs for up to 15s — on CI, `docker logs` sometimes lags
    # behind the container's Python stdout even after healthz is green.
    local name="$1"
    local needle="$2"
    local deadline=$(( $(date +%s) + 15 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if docker logs "$name" 2>&1 | grep -qF "$needle"; then
            return 0
        fi
        sleep 0.5
    done
    docker logs "$name" 2>&1 | tail -30 >&2
    fail "expected log line '$needle' not found in $name"
}

assert_log_not_contains() {
    local name="$1"
    local needle="$2"
    if docker logs "$name" 2>&1 | grep -qF "$needle"; then
        fail "forbidden log line '$needle' found in $name"
    fi
}

# Build a test-only image whose `claude` on PATH is the mock CLI from
# test-local/mock-claude-cli/claude. Used by the engagement e2e blocks when
# CASA_USE_MOCK_CLAUDE=1.
#
# It must replace what `claude` RESOLVES to, not just /usr/bin/claude: npm
# installs the real CLI at /usr/local/bin/claude (a symlink into
# node_modules), and /usr/local/bin precedes /usr/bin on PATH — so writing
# only /usr/bin/claude left every "mock-gated" run silently exec'ing the REAL
# CLI, which 401s in CI with no API key. That was invisible because the one
# mock-gated probe still running (test_mcp_restart_survival.sh) made only curl
# calls and never spawned a CLI. The final `grep` is the fix's own guard: the
# BUILD fails if the overlay does not take, rather than the suite passing on a
# CLI it did not mean to run.
build_image_with_mock_cli() {
    local repo_root
    repo_root="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
    local tag="$IMAGE"
    build_image          # build the standard Casa image first

    # Overlay the mock CLI on top with a tiny derivative Dockerfile.
    local derivative
    derivative="$(mktemp -d)"
    cat > "$derivative/Dockerfile" <<'EOF'
ARG BASE
FROM ${BASE}
COPY mock-claude /usr/bin/claude
RUN set -e; \
    chmod +x /usr/bin/claude; \
    resolved="$(command -v claude)"; \
    [ "$resolved" = /usr/bin/claude ] || ln -sf /usr/bin/claude "$resolved"; \
    head -3 "$(command -v claude)" | grep -q 'Mock `claude` CLI'
EOF
    cp "$repo_root/test-local/mock-claude-cli/claude" "$derivative/mock-claude"
    MSYS_NO_PATHCONV=1 docker build -q --build-arg "BASE=${tag}" \
        -t "$tag" "$derivative" >/dev/null \
        || fail "mock CLI overlay build failed (is \`claude\` still resolving to the real CLI?)"
    rm -rf "$derivative"
    log "Overlaid mock claude CLI on $tag"
}

# wait_for_text_in_log <container> <pattern> [timeout_s]
# Polls `docker logs` until *pattern* (grep -E, fixed regex ok) appears.
# Returns 0 on match, 1 on timeout. Unlike assert_log_contains this never
# fails the suite — callers decide what to do.
wait_for_text_in_log() {
    local name="$1"
    local pattern="$2"
    local timeout="${3:-30}"
    local end=$(( $(date +%s) + timeout ))
    while [ "$(date +%s)" -lt "$end" ]; do
        if docker logs "$name" 2>&1 | grep -Eq "$pattern"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# assert_file_contains <container> <path> <needle> <label>
# Fails the suite if *needle* is not in <path> inside <container>.
assert_file_contains() {
    local name="$1"
    local path="$2"
    local needle="$3"
    local label="$4"
    if MSYS_NO_PATHCONV=1 docker exec "$name" grep -qF "$needle" "$path"; then
        pass "$label"
    else
        MSYS_NO_PATHCONV=1 docker exec "$name" cat "$path" 2>&1 | tail -20 >&2 || true
        fail "$label (grep for '$needle' in $path failed)"
    fi
}

# start_mock_telegram_server [port]
# Spawns the mock Telegram server backing the engagement e2e tests.
# Echoes the PID of the spawned python process on stdout (caller traps cleanup).
# Honors $MOCK_TG_PORT (default 8081) so multiple suites can pick distinct
# ports if ever run in parallel.
# Returns 0 on success, 1 on timeout (after which caller should fail loudly).
start_mock_telegram_server() {
    local port="${1:-${MOCK_TG_PORT:-8081}}"
    local repo_root py
    repo_root="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
    # server.py imports aiohttp — resolve a venv-first interpreter (#270).
    # Explicit `|| return 1`: the only caller is
    # `MOCK_PID="$(start_mock_telegram_server …)" || fail …`, and bash disables
    # errexit for the left side of a `||`, so a resolver failure would sail on,
    # background an empty command, and surface 3s later as a bogus "mock TG
    # never started" instead of the real missing-aiohttp diagnostic.
    py="$(e2e_python)" || return 1
    "$py" "$repo_root/test-local/e2e/mock_telegram/server.py" \
        >/tmp/mock-tg.log 2>&1 &
    local pid=$!
    local i
    for i in $(seq 1 10); do
        if curl -sf "http://localhost:${port}/_inspect" >/dev/null 2>&1; then
            echo "$pid"
            return 0
        fi
        sleep 0.3
    done
    kill "$pid" 2>/dev/null || true
    return 1
}
