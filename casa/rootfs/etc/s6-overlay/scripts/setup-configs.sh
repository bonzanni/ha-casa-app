#!/command/with-contenv bashio
# 5.5 item 3: strip ANSI from bashio output for clean docker logs.
export BASHIO_LOG_NO_COLORS=true
export NO_COLOR=1

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
         "$CONFIG_DIR/bindings" \
         "$CONFIG_DIR/bindings/resident-assistant" \
         "$CONFIG_DIR/bindings/resident-butler" \
         "$CONFIG_DIR/bindings/resident-concierge" \
         "$CONFIG_DIR/specialists" \
         "$CONFIG_DIR/specialists/.staging" \
         "$CONFIG_DIR/specialists/.roles-overlay" \
         "$CONFIG_DIR/schema" \
         "$DATA_DIR/sdk-sessions" \
         "$DATA_DIR/casa-s6-services" \
         "$DATA_DIR/engagements"

# Plugin media outbox (v0.73.0): a shared /data drop-box for producer plugins;
# send_media claims files out of it. Private .claims/ holds in-flight claims.
# Restrictive 0770 (root + root group); CASA_PLUGIN_OUTBOX_DIR is exported into
# the s6 container environment so casa-main sees it at boot.
mkdir -p "$DATA_DIR/plugin-outbox/.claims"
chmod 0770 "$DATA_DIR/plugin-outbox" "$DATA_DIR/plugin-outbox/.claims"
printf '%s' "$DATA_DIR/plugin-outbox" \
    > /run/s6/container_environment/CASA_PLUGIN_OUTBOX_DIR
bashio::log.info "Plugin outbox ready: $DATA_DIR/plugin-outbox"

# Authorization-callback spool: a shared /data drop-box for the
# public /callback/ endpoint's minted-state + result files. Same restrictive
# 0770 idiom as the plugin outbox above; CASA_CALLBACK_SPOOL_ROOT is exported
# into the s6 container environment so casa-main sees it at boot
# (callback_spool.spool_root() honours the override, default /data/callbacks).
mkdir -p "$DATA_DIR/callbacks"
chmod 0770 "$DATA_DIR/callbacks"
printf '%s' "$DATA_DIR/callbacks" \
    > /run/s6/container_environment/CASA_CALLBACK_SPOOL_ROOT
bashio::log.info "Callback spool ready: $DATA_DIR/callbacks"

# Pre-1.0.0 doctrine (see memory/feedback_ship_gate_doctrine.md): no
# migration blocks in this script. Breaking changes just update the
# defaults; the overlay at /config/ is expected to
# be wiped across updates in development mode. This keeps
# setup-configs.sh lean. Revisit when v1.0.0 ships.
# (A stored add-on option whose key was removed from config.yaml's
# schema: only draws a Supervisor WARN; casa ignores unknown keys.)

# Seed schemas (overwrite on every boot — schemas ship with the Casa
# image and the image is the source of truth; hand-edits under
# /config/schema/ get clobbered by design).
if [ -d "$DEFAULTS_DIR/schema" ]; then
    cp "$DEFAULTS_DIR/schema"/*.json "$CONFIG_DIR/schema/" 2>/dev/null || true
    bashio::log.info "Refreshed schema files"
fi

# ------------------------------------------------------------------
# Initialize git repo (idempotent) + snapshot manual edits.
# ------------------------------------------------------------------

if ! command -v git >/dev/null 2>&1; then
    bashio::log.warning "git not installed — skipping config repo init"
elif [ ! -d "$CONFIG_DIR/.git" ]; then
    cd "$CONFIG_DIR"
    git init -q
    git config user.email "casa@local"
    git config user.name  "Casa"
    # Whitelist mirrors config_git._GITIGNORE_CONTENT — keep in sync; the
    # python side reconciles drift on every boot (P-3, v0.69.1).
    cat > .gitignore <<'EOF'
# Casa config repo — track configs only.
*
!agents/
!agents/**
!policies/
!policies/**
!bindings/
!bindings/**
!schema/
!schema/**
# Unified plugin architecture (v0.71.0): the registry is config — the single
# plugin-assignment authority — and versioning it gives an audit trail.
# ONLY registry.json: the artifact store and staging under plugins/ are
# content-addressed binaries, never tracked.
!plugins/
!plugins/registry.json
plugins/store/
plugins/.staging/
# Installed-specialist data model (Task 13): registry.json is config — same
# audit-trail rationale as plugins/registry.json above. ONLY the per-slug
# active/desired/prior tuples and the top-level registry are tracked; the
# content-addressed component store and staging are binaries, never tracked.
!specialists/
!specialists/registry.json
!specialists/*/active.yaml
!specialists/*/desired.yaml
!specialists/*/active.prior.yaml
specialists/store/
specialists/.staging/
!.gitignore
EOF
    git add -A 2>/dev/null || true
    git commit -qm "initial config snapshot" 2>/dev/null || true
    bashio::log.info "Initialized config git repo at $CONFIG_DIR"
else
    # Idempotent boot-time snapshot of any uncommitted manual edits.
    cd "$CONFIG_DIR"
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        if git add -A && git commit -qm "manual edit (boot-time snapshot)"; then
            bashio::log.info "Snapshotted manual edits in config repo"
        else
            bashio::log.warning "Boot-time config snapshot failed (git error) — reconciler will fall back to .casabak"
        fi
    fi
fi

# ------------------------------------------------------------------
# Default-sync reconciler (three-way merge defaults → /config).
# Spec: docs/superpowers/specs/2026-06-08-config-sync-reconciler-design.md.
# Runs AFTER git-repo init (commit-first pre-sync needs /config/.git) and
# BEFORE svc-casa's load_all_agents. Subsumes the old seed_agent_dir block
# and the warn-only drift-check. Non-fatal by contract.
# ------------------------------------------------------------------
export CASA_CONFIG_DIR="$CONFIG_DIR"
export CASA_DEFAULTS_DIR="$DEFAULTS_DIR"
export CASA_DATA_DIR="$DATA_DIR"
export CASA_IMAGE_VERSION="$(bashio::addon.version 2>/dev/null || echo unknown)"
# D1 (2026-07-09 bug review): config_sync's post-sync boot-parity pass runs the
# real agent loader, which resolves ${PRIMARY_AGENT_MODEL}/${VOICE_AGENT_MODEL}
# in runtime.yaml via resolve_model(). svc-casa/run exports these for the actual
# boot, but this oneshot runs in a separate s6 process that did NOT — so the
# validator saw the literal "${...}" and reported a bogus "Unknown model
# shortname" in config-sync-report.json. Export them here for env-parity with
# boot so the validation is faithful (a genuinely bad model still fails).
_casa_primary_model="$(bashio::config 'primary_agent_model')"
_casa_voice_model="$(bashio::config 'voice_agent_model')"
[ -n "$_casa_primary_model" ] && [ "$_casa_primary_model" != "null" ] || _casa_primary_model=opus
[ -n "$_casa_voice_model" ] && [ "$_casa_voice_model" != "null" ] || _casa_voice_model=haiku
export PRIMARY_AGENT_MODEL="$_casa_primary_model"
export VOICE_AGENT_MODEL="$_casa_voice_model"
python3 /opt/casa/config_sync.py || bashio::log.warning "config_sync exited non-zero (non-fatal)"

# Initialize session registry if missing
if [ ! -f "$DATA_DIR/sessions.json" ]; then
    echo '{}' > "$DATA_DIR/sessions.json"
fi

# Persist CC CLI conversation transcripts across container rebuilds.
# As of v0.37.8 (H-1), HOME is propagated to cc-home via
# /run/s6/container_environment/HOME (see claude-home-propagation
# block below), so the CC CLI writes transcripts to
# cc-home/.claude/projects/ directly. This defensive symlink at
# /root/.claude/projects remains as belt-and-braces in case anything
# is ever invoked with explicit HOME=/root (the prior default).
# Pre-v0.37.8 history: CC CLI used $HOME=/root → /root/.claude/projects;
# /root/ is wiped on every rebuild, so --resume <sid> failed on the
# first turn after a deploy (sessions.json persisted, transcript file
# did not — see agent.py resume-recovery comment).
PERSIST_PROJECTS="$CONFIG_DIR/cc-home/.claude/projects"
mkdir -p "$PERSIST_PROJECTS" /root/.claude
if [ -e /root/.claude/projects ] && [ ! -L /root/.claude/projects ]; then
    cp -R /root/.claude/projects/. "$PERSIST_PROJECTS/" 2>/dev/null || true
    rm -rf /root/.claude/projects
fi
[ -L /root/.claude/projects ] || ln -s "$PERSIST_PROJECTS" /root/.claude/projects

# Webhook/voice authentication is MANDATORY (v0.125.0, #228): the
# `webhook_auth_enabled` toggle is removed, so a secret always exists. Every
# external route already fails closed without one (v0.116.0/v0.117.0, #193) —
# the toggle's only remaining effect was to turn those routes off entirely,
# which is not an operator preference, it is a broken install.
SECRET_FILE="$DATA_DIR/webhook_secret"
USER_SECRET=$(bashio::config 'webhook_secret')
# bashio returns the literal string "null" for an unset optional value.
# Treat it exactly like an empty override so auth gets a random secret.
if [ "$USER_SECRET" = "null" ]; then
    USER_SECRET=""
fi
# GHSA-569r-7crq-xr43: both branches below wrote this file at the 0022 umask
# default (0644), so every per-engagement uid could read the global HMAC secret
# and forge signed /invoke/* requests. umask 077 covers the CREATE; the chmod
# after the block repairs a file that already exists from an earlier version
# (a redirection into an existing path does not change its mode). Casa's own
# private_state.enforce() also repairs it on every boot — this is the earliest
# point it can be right, since setup-configs runs before casa-core.
umask 077
if [ -n "$USER_SECRET" ]; then
    printf '%s' "$USER_SECRET" > "$SECRET_FILE"
elif [ ! -s "$SECRET_FILE" ] || \
     [ "$(cat "$SECRET_FILE" 2>/dev/null)" = "null" ]; then
    # -s, not -f (Sol review): the redirection below truncates the file before
    # the pipeline writes it, so a container killed mid-generation leaves a
    # ZERO-BYTE secret. With auth mandatory since v0.125.0 that is an install
    # no route can authenticate and no option can turn off — an empty file must
    # regenerate, not be trusted. Written to a temp file and moved so the
    # window where the real path is empty does not exist at all.
    _secret_tmp="$SECRET_FILE.tmp.$$"
    if head -c 32 /dev/urandom | base64 | tr -d '=/+' | head -c 48 \
            > "$_secret_tmp" && [ -s "$_secret_tmp" ]; then
        mv -f "$_secret_tmp" "$SECRET_FILE"
        bashio::log.info "Auto-generated webhook secret (see /data/webhook_secret)"
    else
        rm -f "$_secret_tmp"
        bashio::log.error "Failed to generate webhook secret"
    fi
    unset _secret_tmp
fi
umask 022
if [ -e "$SECRET_FILE" ]; then
    chmod 0600 "$SECRET_FILE" \
        || bashio::log.error "failed to tighten $SECRET_FILE — engagements will refuse to start"
fi

# GHSA-569r-7crq-xr43: s6-overlay materialises the add-on's environment into
# /run/s6/container_environment/<NAME> at 0644, and SUPERVISOR_TOKEN/HASSIO_TOKEN
# are Supervisor API bearers — the Supervisor API returns every add-on's stored
# options, so a readable token transitively exposes the Claude OAuth token, the
# GitHub PAT and the 1Password service-account token. The engagement run template
# `unset`s both from the environment; that is theatre while the backing files are
# world-readable. The GITHUB_TOKEN / CLAUDE_CODE_OAUTH_TOKEN blocks below already
# write theirs under `umask 077`; these two are not ours to write, only to tighten.
for _tokfile in SUPERVISOR_TOKEN HASSIO_TOKEN; do
    if [ -f "/run/s6/container_environment/$_tokfile" ]; then
        chmod 0600 "/run/s6/container_environment/$_tokfile" \
            || bashio::log.error "failed to tighten $_tokfile — engagements will refuse to start"
    fi
done
unset _tokfile

# Publish Casa's authenticated endpoint to the companion integration through
# Supervisor discovery. The publisher owns only the returned UUID in /data;
# it reads the selected secret above and never logs or persists that secret.
# Always "true" since v0.125.0 (#228) — the discovery payload field stays in
# the contract the companion integration reads; only the toggle behind it is
# gone. #333: the OP service-account token rides along (scoped to this one
# invocation, same pattern as the op-read blocks below) so an op://-valued
# webhook_secret can be resolved to the value Casa actually verifies with.
_op_tok_discovery="$(bashio::config 'onepassword_service_account_token')"
[ "$_op_tok_discovery" = "null" ] && _op_tok_discovery=""
CASA_DISCOVERY_AUTH_ENABLED=true OP_SERVICE_ACCOUNT_TOKEN="$_op_tok_discovery" \
    python3 /opt/casa/supervisor_discovery.py || \
    bashio::log.warning "Supervisor discovery publisher exited non-zero"
unset _op_tok_discovery

# --- cc-home HOME setup -----------------------------------------------------
# casa-main + the CC CLI both require HOME=cc-home. Plugin materialization
# (bundled-artifact import + registry seed) now lives in the
# init-plugin-store s6 oneshot (plugin_boot.py), which runs AFTER this script
# and BEFORE svc-casa (unified plugin architecture §3.6). The marketplace seed,
# the load-bearing `claude -p noop`, and the marketplace registration/install
# loop are all removed with the marketplace itself.
export HOME=/config/cc-home
mkdir -p "$HOME/.claude"

# === github-token: begin ========================================
# v0.14.9: resolve op://VAULT/GitHub/credential at boot, export to s6
# container env so every supervised service + engagement subprocess
# inherits $GITHUB_TOKEN automatically. The token is consumed by
# /opt/casa/scripts/git-credential-casa.sh (wired in /etc/gitconfig)
# at git auth time — never written to disk.
#
# If 1P credentials aren't configured, leave $GITHUB_TOKEN unset →
# public-only mode: anonymous github clones still work via the
# /etc/gitconfig SSH→HTTPS rewrite; private clones return 404/403.
OP_TOK="$(bashio::config 'onepassword_service_account_token')"
VAULT="$(bashio::config 'onepassword_default_vault')"
GH_TOKEN=""
if [ -n "$OP_TOK" ] && [ "$OP_TOK" != "null" ] \
   && [ -n "$VAULT" ] && [ "$VAULT" != "null" ]; then
    GH_TOKEN=$(OP_SERVICE_ACCOUNT_TOKEN="$OP_TOK" \
        op read "op://${VAULT}/GitHub/credential" 2>/dev/null) || GH_TOKEN=""
fi
if [ -n "$GH_TOKEN" ]; then
    # s6-overlay's /run/s6/container_environment/<NAME> is read once at
    # service-spawn time and merged into each child process's env.
    # File mode 0600 root-only — same protection level as /data/ secrets.
    umask 077
    printf "%s" "$GH_TOKEN" > /run/s6/container_environment/GITHUB_TOKEN
    umask 022
    bashio::log.info "GitHub access: token-authenticated (public + private per PAT scope)"
else
    rm -f /run/s6/container_environment/GITHUB_TOKEN
    bashio::log.info "GitHub access: anonymous (public only)"
fi
unset OP_TOK VAULT GH_TOKEN
# === github-token: end ==========================================

# === claude-oauth-token: begin ==================================
# K-1 (v0.34.1): propagate Claude Code OAuth token to engagement
# subprocesses launched by claude_code_driver. Mirror of the
# GITHUB_TOKEN block above. Pre-fix the token was only exported into
# svc-casa's process env (svc-casa/run:13), which feeds casa_core
# itself but NOT s6-rc child services launched via `with-contenv`
# (which read /run/s6/container_environment/). Result: every
# claude_code_driver subprocess (plugin-developer) got
# "Not logged in · Please run /login" and produced no useful output.
# Latently broken since v0.13.0 (Plan 4a) — ~8 days.
#
# bug-review-2026-05-01-exploration4.md::K-1 has the full evidence
# chain. Fix shape: same op:// resolution path as GITHUB_TOKEN above,
# write the (possibly-resolved) token to container_environment with
# mode 0600.
CC_OAUTH="$(bashio::config 'claude_oauth_token')"
if [ -n "$CC_OAUTH" ] && [ "$CC_OAUTH" != "null" ]; then
    case "$CC_OAUTH" in
        op://*)
            OP_TOK2="$(bashio::config 'onepassword_service_account_token')"
            if [ -n "$OP_TOK2" ] && [ "$OP_TOK2" != "null" ]; then
                CC_OAUTH=$(OP_SERVICE_ACCOUNT_TOKEN="$OP_TOK2" \
                    op read "$CC_OAUTH" 2>/dev/null) || CC_OAUTH=""
            else
                CC_OAUTH=""
            fi
            unset OP_TOK2
            ;;
    esac
fi
if [ -n "$CC_OAUTH" ] && [ "$CC_OAUTH" != "null" ]; then
    umask 077
    printf "%s" "$CC_OAUTH" > /run/s6/container_environment/CLAUDE_CODE_OAUTH_TOKEN
    umask 022
    bashio::log.info "Claude OAuth: token propagated to engagement subprocesses"
else
    rm -f /run/s6/container_environment/CLAUDE_CODE_OAUTH_TOKEN
    bashio::log.warning "Claude OAuth not configured — claude_code_driver engagements will fail (K-1)"
fi
unset CC_OAUTH
# === claude-oauth-token: end ====================================

# === claude-home-propagation: begin =============================
# H-1 (v0.37.8): propagate HOME=cc-home to every s6-supervised service +
# child subprocess. A shell-level `export HOME=...` only governs this
# script's own process; casa-main and svc-casa-mcp boot with HOME=/root
# unless we write to /run/s6/container_environment/. cc-home is still the
# CC CLI's home for residents/specialists (SDK plugin loading via
# --plugin-dir, agent-home settings) and engagement subprocesses.
printf '%s' "/config/cc-home" \
    > /run/s6/container_environment/HOME
bashio::log.info "HOME propagated to s6 services: /config/cc-home"
# === claude-home-propagation: end ===============================

# Plugin materialization (bundled-artifact import → content-addressed store,
# registry seed) moved to the init-plugin-store s6
# oneshot (plugin_boot.py) under the unified plugin architecture (§3.6). The
# old /opt/claude-seed → cc-home seed-copy + `claude plugin enable` loop is
# deleted with the marketplace.

# --- Plan 4b: plugin-runtime tool dir + PATH propagation (P-9) -------------
# Ensure the persistent tools bin dir exists, and add it to PATH for every
# s6-supervised service (casa-main, svc-casa-mcp, engagements). Writing to
# /run/s6/container_environment/PATH is how s6-overlay propagates env to
# children; /etc/profile.d/* is NOT sourced by non-interactive services.
TOOLS_ROOT=/config/tools
TOOLS_BIN="$TOOLS_ROOT/bin"
mkdir -p "$TOOLS_BIN"

# Merge TOOLS_BIN into s6 container env PATH. NOTE: it is prepended ahead of
# the ENTIRE image PATH including /opt/casa/venv/bin — intentional for
# engagement tool overrides; core services must therefore exec the venv
# interpreter by absolute path (/opt/casa/venv/bin/python3), never bare python3.
CURRENT_PATH="${PATH}"
if ! printf "%s" "$CURRENT_PATH" | grep -q "^\(.*:\)\?${TOOLS_BIN}\(:\|$\)"; then
    NEW_PATH="$TOOLS_BIN:$CURRENT_PATH"
    printf "%s" "$NEW_PATH" > /run/s6/container_environment/PATH
fi

# Drop any legacy profile.d leftover from earlier drafts. Safe on fresh install.
rm -f /etc/profile.d/casa-tools.sh

# --- G7 (v0.95.1): keep interpreter bytecode OUT of plugin artifacts -------
# A Python plugin MCP server used to bytecode-cache into its own frozen,
# checksummed store artifact (-> corrupt_artifact on the next health regen;
# and a writable cache inside an artifact is the foothold for a crafted-.pyc
# shadow attack). Redirect ALL python bytecode caches to a disposable
# location for every s6-supervised service and their children (the CC CLI
# and plugin MCP servers inherit this).
mkdir -p /tmp/casa-pycache
printf "%s" /tmp/casa-pycache > /run/s6/container_environment/PYTHONPYCACHEPREFIX

# --- Plan 4b: system-requirements reconciliation (§4.3.4) -----------------
# Reconciler runs the declared install strategy for every plugin tool that is
# missing after upgrade (persistent volume survives, but failures / user-wipes
# happen). Non-blocking — degrades affected plugins, never crashes boot.
MANIFEST=/config/system-requirements.yaml
STATUS_FILE=/config/system-requirements.status.yaml
if [ -f "$MANIFEST" ]; then
    python3 /opt/casa/scripts/reconcile_system_requirements.py \
        --manifest "$MANIFEST" \
        --tools-root "$TOOLS_ROOT" \
        --status-file "$STATUS_FILE" \
        --log-level warning \
        || bashio::log.warning \
          "system-requirements reconciliation had failures — see $STATUS_FILE"
fi

bashio::log.info "Configuration setup complete."
