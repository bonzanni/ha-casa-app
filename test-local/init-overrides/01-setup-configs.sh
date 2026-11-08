#!/bin/sh
# Test override: reads options.json directly instead of bashio.
# Structurally mirrors casa/rootfs/etc/s6-overlay/scripts/setup-configs.sh.
CONFIG_DIR="/config"
DATA_DIR="/data"
DEFAULTS_DIR="/opt/casa/defaults"

# ------------------------------------------------------------------
# Directory scaffolding (idempotent).
# ------------------------------------------------------------------

mkdir -p "$CONFIG_DIR/agents" \
         "$CONFIG_DIR/agents/specialists" \
         "$CONFIG_DIR/agents/executors" \
         "$CONFIG_DIR/policies" \
         "$CONFIG_DIR/schema" \
         "$CONFIG_DIR/workspace/.claude/skills" \
         "$CONFIG_DIR/workspace/plugins" \
         "$CONFIG_DIR/workspace/mcp-servers" \
         "$DATA_DIR/sdk-sessions"

# ------------------------------------------------------------------
# Seed defaults on first boot (directory-copy).
# ------------------------------------------------------------------

seed_agent_dir() {
    src="$1"
    dst="$2"
    if [ -d "$src" ] && [ ! -d "$dst" ]; then
        cp -r "$src" "$dst"
        echo "[INFO] Seeded agent dir: $(basename "$dst")"
    fi
}

if [ -d "$DEFAULTS_DIR/agents" ]; then
    for src in "$DEFAULTS_DIR/agents"/*/; do
        [ -d "$src" ] || continue
        name=$(basename "$src")
        [ "$name" = "specialists" ] && continue
        [ "$name" = "executors" ] && continue
        seed_agent_dir "$src" "$CONFIG_DIR/agents/$name"
    done
fi

if [ -d "$DEFAULTS_DIR/agents/specialists" ]; then
    for src in "$DEFAULTS_DIR/agents/specialists"/*/; do
        [ -d "$src" ] || continue
        name=$(basename "$src")
        seed_agent_dir "$src" "$CONFIG_DIR/agents/specialists/$name"
    done
fi

if [ -d "$DEFAULTS_DIR/agents/executors" ]; then
    for src in "$DEFAULTS_DIR/agents/executors"/*/; do
        [ -d "$src" ] || continue
        name=$(basename "$src")
        seed_agent_dir "$src" "$CONFIG_DIR/agents/executors/$name"
    done
fi

# Seed shared policy library.
if [ ! -f "$CONFIG_DIR/policies/disclosure.yaml" ] \
   && [ -f "$DEFAULTS_DIR/policies/disclosure.yaml" ]; then
    cp "$DEFAULTS_DIR/policies/disclosure.yaml" \
       "$CONFIG_DIR/policies/disclosure.yaml"
    echo "[INFO] Seeded policies/disclosure.yaml"
fi

if [ ! -f "$CONFIG_DIR/policies/scopes.yaml" ] \
   && [ -f "$DEFAULTS_DIR/policies/scopes.yaml" ]; then
    cp "$DEFAULTS_DIR/policies/scopes.yaml" \
       "$CONFIG_DIR/policies/scopes.yaml"
    echo "[INFO] Seeded policies/scopes.yaml"
fi

# Pre-1.0.0 doctrine: no migration blocks. Mirrors prod setup-configs.sh.

# Seed schemas (overwrite on every boot).
if [ -d "$DEFAULTS_DIR/schema" ]; then
    cp "$DEFAULTS_DIR/schema"/*.json "$CONFIG_DIR/schema/" 2>/dev/null || true
    echo "[INFO] Refreshed schema files"
fi

# ------------------------------------------------------------------
# Initialize git repo (idempotent) + snapshot manual edits.
# ------------------------------------------------------------------

if ! command -v git >/dev/null 2>&1; then
    echo "[WARN] git not installed — skipping config repo init"
elif [ ! -d "$CONFIG_DIR/.git" ]; then
    cd "$CONFIG_DIR"
    git init -q
    git config user.email "casa@local"
    git config user.name  "Casa"
    cat > .gitignore <<'EOF'
# Track configs only.
*
!agents/
!agents/**
!policies/
!policies/**
!schema/
!schema/**
!.gitignore
EOF
    git add .gitignore agents/ policies/ schema/ 2>/dev/null || true
    git commit -qm "initial config snapshot" 2>/dev/null || true
    echo "[INFO] Initialized config git repo at $CONFIG_DIR"
else
    cd "$CONFIG_DIR"
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        git add -A
        git commit -qm "manual edit (boot-time snapshot)"
        echo "[INFO] Snapshotted manual edits in config repo"
    fi
fi

# Skip repo sync in local test mode
echo "[INFO] Skipping repo sync (local test mode)."

if [ ! -f "$DATA_DIR/sessions.json" ]; then
    echo '{}' > "$DATA_DIR/sessions.json"
fi

# Materialize the webhook secret, mirroring the real
# rootfs/etc/s6-overlay/scripts/setup-configs.sh (#262).
#
# Until v0.125.0 (#228) this branched on a `webhook_auth_enabled` option and
# `rm -f`'d the secret when it was absent or false. That option is GONE and
# auth is MANDATORY: the real script always materializes a secret, so the
# secretless container the old else-branch produced was a state production
# could not be in. Worse, the default fixture omits the key, so `// false`
# made that impossible state the DEFAULT for every plain start_container.
SECRET_FILE="$DATA_DIR/webhook_secret"

if [ "${E2E_FORCE_NO_WEBHOOK_SECRET:-0}" = "1" ]; then
    # Test-only escape hatch, opted into per-container via
    # `docker run -e E2E_FORCE_NO_WEBHOOK_SECRET=1`. Production reaches the
    # secretless state only when generation FAILS (the real script logs
    # "Failed to generate webhook secret" and leaves none), and the voice
    # routes must still fail closed there — test_voice_sse.sh V-1/V-2 cover
    # exactly that. Skip generation entirely; do not fall through.
    rm -f "$SECRET_FILE"
    echo "[WARN] E2E_FORCE_NO_WEBHOOK_SECRET=1 — booting SECRETLESS (test-only; every authenticated route must refuse)"
else
    USER_SECRET=$(jq -r '.webhook_secret // empty' /data/options.json)
    if [ -n "$USER_SECRET" ]; then
        printf '%s' "$USER_SECRET" > "$SECRET_FILE"
    elif [ ! -s "$SECRET_FILE" ] || \
         [ "$(cat "$SECRET_FILE" 2>/dev/null)" = "null" ]; then
        # -s, not -f, matching the real script: the redirection truncates
        # before the pipeline writes, so a container killed mid-generation
        # leaves a ZERO-BYTE secret that must regenerate rather than be
        # trusted. The literal "null" is the other invalid value the real
        # script rejects — bashio yields that string for an unset optional, so
        # a boot that persisted it must not have it accepted as a real key.
        # Temp file + mv so the real path is never transiently empty.
        _secret_tmp="$SECRET_FILE.tmp.$$"
        if head -c 32 /dev/urandom | base64 | tr -d '=/+' | head -c 48 \
                > "$_secret_tmp" && [ -s "$_secret_tmp" ]; then
            mv -f "$_secret_tmp" "$SECRET_FILE"
            echo "[INFO] Auto-generated webhook secret (see /data/webhook_secret)"
        else
            rm -f "$_secret_tmp"
            echo "[ERROR] Failed to generate webhook secret"
        fi
        unset _secret_tmp
    fi
fi

# v0.71.0: no seed-copy in the test override. Plugin materialization is the
# init-plugin-store s6 oneshot (plugin_boot.py), which runs unmodified in the
# test image (since v0.349.0 the image carries no bundled plugins).

echo "[INFO] Configuration setup complete (local test mode)."
