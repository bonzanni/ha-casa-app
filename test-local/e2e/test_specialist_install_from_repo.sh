#!/usr/bin/env bash
# ============================================================================
# Task 16 — FINAL GATE e2e: install-from-repo -> delegatable specialist, and a
# resident persona swap forcing a new session epoch, both inside the REAL Casa
# container.
#
# CONTROLLER RECONCILIATION (disclosed in test-16 report):
#   * The plan brief described a pytest harness with fixtures that DO NOT EXIST
#     in this repo (casa_container, local_git_fixture_repo, delegate_to_agent,
#     restart_supervised, ...). This shell script delivers the brief's
#     LOAD-BEARING assertions in the real test-local/e2e idiom instead
#     (common.sh: build_image/start_container/wait_healthy).
#   * The NETWORK SEAM ONLY is substituted: specialist_install.resolve_and_fetch
#     / persona_install.resolve_and_fetch resolve a ref via the GitHub commits
#     API + git fetch (plugin_store.resolve_ref/fetch_commit_tree) — they cannot
#     run hermetically. A docker-exec'd python driver monkeypatches that single
#     function to copy the committed static fixture into `dest`, EXACTLY as the
#     unit tests do (_stub_resolve_and_fetch, tests/test_specialist_install.py).
#     Everything downstream — manifest/marker validation, dependency closure,
#     the consent gate, CAS persistence, persona<->role compile, tuple
#     activation, operational-file materialize — is the REAL production code.
#   * The reload + live-index refresh + status query + persona render all run
#     THROUGH THE RUNNING SERVER over its internal admin unix socket
#     (/run/casa/internal.sock: /admin/reload -> reload.dispatch, the same path
#     as the casa_reload MCP tool; /admin/specialist/status; /admin/personality/
#     render). That is the Correction #1 runtime proof: a specialist that only
#     produced a CAS entry with no live-index/roles-overlay pickup would show
#     state="not_installed" after reload and FAIL C-4 below.
#   * Consent is recorded directly via the Ack stores inside the container (the
#     DM Approve/Deny keyboard is unit-covered by the *_install_consent tests);
#     the e2e's job is the runtime install/reload/swap proof, not the Telegram
#     tap. (Controller-authorized scope decision.)
# ============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"
NAME="casa-install-repo-$$"
SOCK="/run/casa/internal.sock"

cleanup_all() {
    stop_container "$NAME" >/dev/null 2>&1 || true
}
trap cleanup_all EXIT

# curl_sock <json-path> <body> -> POST body to the internal admin socket, print
# response body; fail the suite on a non-2xx status.
curl_sock() {
    local path="$1" body="$2"
    MSYS_NO_PATHCONV=1 docker exec "$NAME" curl -sf \
        --unix-socket "$SOCK" \
        -X POST -H 'Content-Type: application/json' \
        -d "$body" "http://localhost${path}"
}

# curl_sock_raw <path> <body> -> like curl_sock but tolerate a non-2xx status
# (returns the body regardless; used for the pre-reload "not_installed" probe).
curl_sock_raw() {
    local path="$1" body="$2"
    MSYS_NO_PATHCONV=1 docker exec "$NAME" curl -s \
        --unix-socket "$SOCK" \
        -X POST -H 'Content-Type: application/json' \
        -d "$body" "http://localhost${path}"
}

# json_get <json> <python-expr over `d`> -> print the evaluated value.
json_get() { printf '%s' "$1" | python3 -c "import json,sys; d=json.load(sys.stdin); print($2)"; }

build_image

start_container "$NAME" >/dev/null
wait_healthy "$NAME"

# Wait for casa-main's internal admin socket (distinct from healthz).
for _ in $(seq 1 20); do
    if MSYS_NO_PATHCONV=1 docker exec "$NAME" test -S "$SOCK"; then break; fi
    sleep 1
done
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -S "$SOCK" \
    || fail "internal admin socket $SOCK never appeared"

# Stage the committed static fixtures into the container.
MSYS_NO_PATHCONV=1 docker exec "$NAME" mkdir -p /tmp/fixtures
MSYS_NO_PATHCONV=1 docker cp \
    "$REPO_ROOT/test-local/fixtures/specialist-components/mtg-test" \
    "$NAME:/tmp/fixtures/mtg-test"
MSYS_NO_PATHCONV=1 docker cp \
    "$REPO_ROOT/test-local/fixtures/personas/alt-butler-tina" \
    "$NAME:/tmp/fixtures/alt-butler-tina"

# ============================================================
# C: install-from-repo makes a DELEGATABLE specialist
# ============================================================
log "C: install-from-repo -> delegatable specialist (mtg-test)"

# C-1: baseline — the running server does NOT know mtg-test yet.
BEFORE_STATUS="$(curl_sock_raw /admin/specialist/status '{"slug":"mtg-test"}')"
log "C-1 pre-install status: $BEFORE_STATUS"
[ "$(json_get "$BEFORE_STATUS" 'd.get("state")')" = "not_installed" ] \
    || fail "C-1: mtg-test unexpectedly already known before install: $BEFORE_STATUS"
pass "C-1 mtg-test unknown before install"

# C-2: drive the REAL install pipeline (fetch-seam stubbed) via a docker-exec
# python driver: inspect -> record consent ack -> commit -> active.
log "C-2: inspect + consent + commit (real specialist_install pipeline)"
INSTALL_OUT="$(MSYS_NO_PATHCONV=1 docker exec -i "$NAME" python3 - <<'PY'
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/opt/casa")

import specialist_install
from specialist_install import (
    commit_specialist_install,
    inspect_specialist_repo,
)
from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

FIXTURE = Path("/tmp/fixtures/mtg-test")


def _stub(repo, ref, subdir, dest, *, expected_revision=None):
    # Network seam only: copy the pre-validated component tree into `dest`,
    # exactly like tests/test_specialist_install.py's _stub_resolve_and_fetch.
    shutil.copytree(FIXTURE, dest)
    return "a" * 40


specialist_install.resolve_and_fetch = _stub

res = inspect_specialist_repo("casa-test/mtg-test", "main")
assert res.slug == "mtg-test", res.slug
assert res.component_id == "casa-test/mtg-test", res.component_id

acks = SpecialistInstallAckStore()
# Whole-branch A: inspect now ALWAYS mints a source receipt (plugin-less
# components included), so res.receipt_digest is non-empty and the real commit
# folds it into the consent identity. The recorded ack MUST carry the same
# receipt_digest or commit's recomputed identity mismatches -> consent_missing.
identity = install_consent_identity(
    component_id=res.component_id, version=res.version,
    root_digest=res.root_digest, slug=res.slug,
    receipt_digest=res.receipt_digest)
acks.record(identity=identity, component_id=res.component_id, version=res.version,
            component_checksum=res.root_digest, slug=res.slug,
            receipt_digest=res.receipt_digest)

instance = commit_specialist_install(
    inspection=res, config={}, secret_names_provided=frozenset(), acks=acks)
assert instance.state == "active", f"state={instance.state} err={instance.last_activation_error}"
print("COMMIT_STATE", instance.state)
print("DRIVER_C_OK")
PY
)"
log "C-2 driver output: $(printf '%s' "$INSTALL_OUT" | tr '\n' '|')"
printf '%s' "$INSTALL_OUT" | grep -q "DRIVER_C_OK" \
    || fail "C-2: install driver did not reach active commit"
pass "C-2 committed mtg-test as active (real CAS/compile/activate)"

# The materialized operational files landed on disk (roles overlay).
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -f /config/agents/specialists/mtg-test/character.yaml \
    || fail "C-2: mtg-test operational files were not materialized"

# C-3: reload the RUNNING server's agents scope — same dispatch as
# casa_reload(scope="agents"). THIS is the Correction #1 wiring under test.
log "C-3: casa_reload(scope=agents) via /admin/reload"
RELOAD_OUT="$(curl_sock /admin/reload '{"scope":"agents"}')"
log "C-3 reload result: $RELOAD_OUT"
[ "$(json_get "$RELOAD_OUT" 'd.get("status")')" = "ok" ] \
    || fail "C-3: reload did not report ok: $RELOAD_OUT"
# Corroboration (soft): the agents sweep reports the newly delegatable slug.
if printf '%s' "$RELOAD_OUT" | grep -q "added_specialist_mtg-test"; then
    pass "C-3 reload reported added_specialist_mtg-test"
else
    log "C-3 note: 'added_specialist_mtg-test' not in actions (status query is authoritative)"
fi

# C-4: THE assertion — the running server now resolves mtg-test as an active,
# delegatable specialist. Regressing the reload/live-index wiring leaves this
# 'not_installed' (unknown_role), failing here.
AFTER_STATUS="$(curl_sock /admin/specialist/status '{"slug":"mtg-test"}')"
log "C-4 post-reload status: $AFTER_STATUS"
[ "$(json_get "$AFTER_STATUS" 'd.get("state")')" = "active" ] \
    || fail "C-4: mtg-test not active after reload (roles-overlay/reload wiring regressed): $AFTER_STATUS"
[ "$(json_get "$AFTER_STATUS" 'd.get("stable_agent_id")')" = "specialist:mtg-test" ] \
    || fail "C-4: unexpected stable_agent_id: $AFTER_STATUS"
[ -n "$(json_get "$AFTER_STATUS" '(d.get("active") or {}).get("binding_digest") or ""')" ] \
    || fail "C-4: active tuple has no binding_digest: $AFTER_STATUS"
pass "C-4 mtg-test is a delegatable active specialist after reload"

# ============================================================
# E: resident persona swap forces a new session epoch (butler)
# ============================================================
log "E: resident persona swap forces a new binding_digest / session epoch"

# E-1: capture butler's compiled-prompt identity digest on the RUNNING server.
RENDER_BEFORE="$(curl_sock /admin/personality/render '{"role":"resident:butler","projection":"text"}')"
DIGEST_BEFORE="$(json_get "$RENDER_BEFORE" 'd["digest"]')"
log "E-1 butler render digest (pre-swap): $DIGEST_BEFORE"
[ -n "$DIGEST_BEFORE" ] || fail "E-1: no compiled-prompt digest for resident:butler"

# E-2: install the override persona (casa/tina@0.2.0) + apply it to butler, and
# read butler's effective binding_digest before vs after via the REAL resident
# personality-compile path (agent_loader.load_all_agents).
log "E-2: persona install + apply_persona_override(resident:butler)"
SWAP_OUT="$(MSYS_NO_PATHCONV=1 docker exec -i "$NAME" python3 - <<'PY'
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/opt/casa")

import agent_loader
import persona_install
from agent_loader import load_all_agents
from policies import load_policies
from persona_install import (
    PersonaInstallAckStore,
    apply_persona_override,
    commit_persona_install,
    inspect_persona_repo,
    persona_install_consent_identity,
)
from personality_binding import InstanceDir
from role_artifact import load_role_artifact
from role_slot import materialize_role

FIXTURE = Path("/tmp/fixtures/alt-butler-tina")


def _stub(repo, ref, subdir, dest, *, expected_revision=None):
    shutil.copytree(FIXTURE, dest)
    return "b" * 40


# persona_install imported resolve_and_fetch by NAME at import time — patch the
# binding it actually calls, not specialist_install's.
persona_install.resolve_and_fetch = _stub


def _butler_binding_digest():
    # Build the PolicyLibrary the same way casa_core does at boot so the
    # resident personality-compile path runs identically to production.
    policy_lib = load_policies("/config/policies/disclosure.yaml")
    cfgs = load_all_agents("/config/agents", policies=policy_lib)
    return cfgs["butler"].binding_digest


bd_before = _butler_binding_digest()
print("BD_BEFORE", bd_before)

res = inspect_persona_repo("casa-test/alt-butler", "main")
assert res.persona_id == "casa/tina" and res.version == "0.2.0", (res.persona_id, res.version)

acks = PersonaInstallAckStore()
ident = persona_install_consent_identity(
    persona_id=res.persona_id, version=res.version, checksum=res.checksum)
acks.record(identity=ident, persona_id=res.persona_id, version=res.version, checksum=res.checksum)
pack = commit_persona_install(inspection=res, acks=acks)

role_dir = Path(agent_loader.DEFAULT_ROLES_DIR) / "resident" / "butler"
role = materialize_role(source=load_role_artifact(role_dir), options={})
# #607: the compile proof is a REQUIRED argument, and this driver must supply
# the same one production does (tools.persona_apply builds it from this exact
# factory) — a test-only stub here would prove the swap works through a path
# no operator can take.
instance_dir = InstanceDir(Path("/config/bindings/resident-butler"))
staged = apply_persona_override(
    target_role_id="resident:butler", persona=pack, role=role,
    instance_dir_root=Path("/config/bindings/resident-butler"),
    candidate_validator=agent_loader.make_candidate_compile_validator(role))
print("OVERRIDE_DIGEST", staged.binding.binding_digest)

# #607: a resident apply STAGES; promotion belongs to boot reconciliation. The
# live pair proves it — desired holds the override while active is still the
# pre-swap binding — which is what makes the tool's restart_required honest.
assert instance_dir.desired() is not None, "override was not staged"
_active = instance_dir.active()
assert _active is not None and _active.binding.binding_digest == bd_before, (
    "apply must not promote: active should still be the pre-swap binding")
print("STAGED_NOT_PROMOTED")

# This reconciles, which is what promotes the staged override to active.
bd_after = _butler_binding_digest()
print("BD_AFTER", bd_after)

assert bd_after != bd_before, "binding_digest did NOT change after persona swap"
assert bd_after == staged.binding.binding_digest, "on-disk override digest mismatch"
print("DRIVER_E_OK")
PY
)"
log "E-2 driver output: $(printf '%s' "$SWAP_OUT" | tr '\n' '|')"
printf '%s' "$SWAP_OUT" | grep -q "DRIVER_E_OK" \
    || fail "E-2: persona-swap driver failed (binding_digest unchanged?): $SWAP_OUT"
pass "E-2 butler binding_digest changed after override apply"

# #607: assert the stage-only step explicitly, not just as a side effect of the
# digest changing — a regression to commit-on-apply would still move the digest.
printf '%s' "$SWAP_OUT" | grep -q "STAGED_NOT_PROMOTED" \
    || fail "E-2: apply promoted the binding instead of staging it: $SWAP_OUT"
pass "E-2 apply staged the override without promoting it"

MSYS_NO_PATHCONV=1 docker exec "$NAME" test -f /config/bindings/resident-butler/active.yaml \
    || fail "E-2: resident-butler override binding was not persisted"

# E-3: restart-to-swap — the RUNNING server has NOT adopted the swap yet (its
# compiled bundle is frozen at boot). Same digest proves restart is required.
RENDER_STILL="$(curl_sock /admin/personality/render '{"role":"resident:butler","projection":"text"}')"
DIGEST_STILL="$(json_get "$RENDER_STILL" 'd["digest"]')"
[ "$DIGEST_STILL" = "$DIGEST_BEFORE" ] \
    || fail "E-3: running server changed butler identity WITHOUT a restart: $DIGEST_STILL"
pass "E-3 running server unchanged pre-restart (restart-to-swap semantics)"

# E-4: real container restart, then re-probe the running server's compiled
# identity. THE assertion: the swap took effect -> a new session epoch.
log "E-4: docker restart (real restart) then re-probe butler identity"
MSYS_NO_PATHCONV=1 docker restart "$NAME" >/dev/null
wait_healthy "$NAME"
for _ in $(seq 1 20); do
    if MSYS_NO_PATHCONV=1 docker exec "$NAME" test -S "$SOCK"; then break; fi
    sleep 1
done
RENDER_AFTER="$(curl_sock /admin/personality/render '{"role":"resident:butler","projection":"text"}')"
DIGEST_AFTER="$(json_get "$RENDER_AFTER" 'd["digest"]')"
log "E-4 butler render digest (post-restart): $DIGEST_AFTER"
[ -n "$DIGEST_AFTER" ] || fail "E-4: no compiled-prompt digest after restart"
[ "$DIGEST_AFTER" != "$DIGEST_BEFORE" ] \
    || fail "E-4: butler identity did NOT change after restart — persona swap did not force a new epoch"
pass "E-4 butler compiled identity changed after restart (new session epoch)"

# Bonus: mtg-test survived the real restart as an active install.
POST_RESTART_STATUS="$(curl_sock /admin/specialist/status '{"slug":"mtg-test"}')"
[ "$(json_get "$POST_RESTART_STATUS" 'd.get("state")')" = "active" ] \
    || fail "post-restart: mtg-test no longer active: $POST_RESTART_STATUS"
pass "mtg-test still active after a real restart"

pass "ALL PASS — specialist install-from-repo + resident persona-swap epoch"
