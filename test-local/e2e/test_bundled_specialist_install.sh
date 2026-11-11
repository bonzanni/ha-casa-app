#!/usr/bin/env bash
# ============================================================================
# Task 13 (specialist-bundled-plugins plan) — e2e: the ONE-FLOW bundled-
# specialist install (inspect -> consent -> commit) publishes an OWNED plugin
# registry entry (`<slug>.<name>` targeting `specialist:<slug>`) and the
# uninstall cascade removes exactly that owned set while:
#   - retaining the published artifact's bytes on disk (CAS/GC-root policy —
#     nothing here ever runs a sweep), and
#   - leaving a pre-seeded OPERATOR-owned plugin (no `owner` field) that also
#     targets the slug completely untouched.
#
# Harness mirrors test_specialist_install_from_repo.sh exactly (build_image/
# start_container/wait_healthy from common.sh; the internal admin unix socket
# for /admin/reload + /admin/specialist/status; a docker-exec'd python driver
# that monkeypatches ONLY the network seam, `specialist_install.resolve_and_
# fetch`, to copy the committed static fixture — everything downstream is the
# REAL production code: manifest/dependency-closure validation, the trusted
# source receipt (Task 8/10), the consent-identity tuple (component_id,
# version, slug, root_digest, receipt_digest — specialist_install_consent.
# install_consent_identity), the journaled bundle transaction (Task 10's
# commit_specialist_install/uninstall_specialist bundle=True path), and the
# CAS/registry/tuple writes).
#
# Consent is recorded directly via SpecialistInstallAckStore inside the
# container (the DM Approve/Deny keyboard itself is unit-covered by the
# specialist_install_consent tests) — this e2e's job is proving the real
# install/uninstall pipeline against the running container's actual
# filesystem + admin routes, not the Telegram tap.
# ============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"
NAME="casa-bundled-specialist-$$"
SOCK="/run/casa/internal.sock"

cleanup_all() {
    stop_container "$NAME" >/dev/null 2>&1 || true
}
trap cleanup_all EXIT

# curl_sock <path> <body> -> POST body to the internal admin socket, print
# response body; fail the suite on a non-2xx status.
curl_sock() {
    local path="$1" body="$2"
    MSYS_NO_PATHCONV=1 docker exec "$NAME" curl -sf \
        --unix-socket "$SOCK" \
        -X POST -H 'Content-Type: application/json' \
        -d "$body" "http://localhost${path}"
}

# curl_sock_raw <path> <body> -> like curl_sock but tolerate a non-2xx status.
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

for _ in $(seq 1 20); do
    if MSYS_NO_PATHCONV=1 docker exec "$NAME" test -S "$SOCK"; then break; fi
    sleep 1
done
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -S "$SOCK" \
    || fail "internal admin socket $SOCK never appeared"

# Stage the committed static fixture into the container.
MSYS_NO_PATHCONV=1 docker exec "$NAME" mkdir -p /tmp/fixtures
MSYS_NO_PATHCONV=1 docker cp \
    "$REPO_ROOT/test-local/fixtures/specialist-components/bundletest" \
    "$NAME:/tmp/fixtures/bundletest"

# ============================================================
# A: baseline — the running server does not know `bundletest` yet.
# ============================================================
log "A: baseline — bundletest unknown before install"
BEFORE_STATUS="$(curl_sock_raw /admin/specialist/status '{"slug":"bundletest"}')"
log "A pre-install status: $BEFORE_STATUS"
[ "$(json_get "$BEFORE_STATUS" 'd.get("state")')" = "not_installed" ] \
    || fail "A: bundletest unexpectedly already known before install: $BEFORE_STATUS"
pass "A bundletest unknown before install"

# ============================================================
# B: one-flow install — inspect -> record consent (WITH receipt_digest in the
# identity tuple) -> commit (bundle mode: receipt supplied) -> owned registry
# entry published -> pre-seed an operator-owned survivor targeting the slug.
# ============================================================
log "B: inspect + consent (receipt_digest-bound identity) + bundle commit"
INSTALL_OUT="$(MSYS_NO_PATHCONV=1 docker exec -i "$NAME" python3 - <<'PY'
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/opt/casa")

import specialist_install
import specialist_receipt
import plugin_registry
from specialist_install import commit_specialist_install, inspect_specialist_repo
from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity
from approval_records import values_digest

FIXTURE = Path("/tmp/fixtures/bundletest")


def _stub(repo, ref, subdir, dest, *, expected_revision=None):
    # Network seam only — copy the pre-validated, committed component tree
    # into `dest`, exactly like tests/test_specialist_install.py's
    # _stub_resolve_and_fetch / tests/specialist_fixtures._subdir_stub.
    shutil.copytree(FIXTURE, dest)
    return "c" * 40


specialist_install.resolve_and_fetch = _stub

res = inspect_specialist_repo("casa-test/bundletest", "main")
assert res.slug == "bundletest", res.slug
assert res.component_id == "casa-test/bundletest", res.component_id
assert res.receipt_id, "inspect did not mint a trusted source receipt"
assert res.receipt_digest, "inspect did not bind a receipt_digest"
assert len(res.plugin_resolutions) == 1, res.plugin_resolutions
row = res.plugin_resolutions[0]
assert row.scoped_name == "bundletest.bt", row.scoped_name
assert row.manifest_name == "bt", row.manifest_name

receipt = specialist_receipt.load(res.receipt_id)
assert receipt is not None, "receipt failed to load back by its opaque id"

# Consent identity tuple = (component_id, version, slug, root_digest,
# receipt_digest, values_digest) — specialist_install_consent.
# install_consent_identity. Recorded AFTER inspect (receipt_digest is only
# known once inspect resolves the bundled-plugin closure), mirroring
# test_specialist_bundle_commit.py's _prep helper. A fresh install with no
# settings supplied stages exactly these approved values.
approved = {"config": {}, "secrets": []}
acks = SpecialistInstallAckStore()
identity = install_consent_identity(
    component_id=res.component_id, version=res.version,
    root_digest=res.root_digest, slug=res.slug,
    receipt_digest=res.receipt_digest, values_digest=values_digest(approved),
)
acks.record(identity=identity, component_id=res.component_id, version=res.version,
            component_checksum=res.root_digest, slug=res.slug,
            receipt_digest=res.receipt_digest, values_digest=values_digest(approved))

instance, txn = commit_specialist_install(
    inspection=res, receipt=receipt, config={}, secret_names_provided=frozenset(),
    acks=acks, approved_values=approved,
)
assert instance.state == "active", f"state={instance.state} err={instance.last_activation_error}"

import specialist_bundle_journal
specialist_bundle_journal.complete(txn.journal_path)

reg = plugin_registry.load_registry(plugin_registry.REGISTRY_PATH)
owned = plugin_registry.owned_entries_for("bundletest", reg)
assert len(owned) == 1, owned
entry = owned[0]
assert entry["name"] == "bundletest.bt", entry
assert entry["manifest_name"] == "bt", entry
assert entry["owner"] == "specialist:bundletest", entry
assert entry["targets"] == ["specialist:bundletest"], entry
artifact_id = entry["artifact_id"]
assert (plugin_registry.STORE_ROOT / "bundletest.bt" / artifact_id).is_dir(), \
    "owned artifact was not published to the store"

# Pre-seed an OPERATOR-installed (unowned) plugin that ALSO targets the
# specialist — must survive the uninstall cascade untouched.
survivor_revision = "git:" + "f" * 40
survivor = {
    "name": "operator-survivor-tool",
    "targets": ["specialist:bundletest"],
    "version": "1.0.0",
    "source": {"type": "github", "repo": "acme/operator-tool", "ref": "v1",
               "revision": survivor_revision, "subdir": ""},
    "artifact_id": plugin_registry.compute_artifact_id(
        repo="acme/operator-tool", revision=survivor_revision, subdir="",
        name="operator-survivor-tool"),
}
reg2 = plugin_registry.load_registry(plugin_registry.REGISTRY_PATH)
reg2.raw["plugins"].append(survivor)
plugin_registry.save_registry(reg2, plugin_registry.REGISTRY_PATH)

print("ARTIFACT_ID", artifact_id)
print("DRIVER_INSTALL_OK")
PY
)"
log "B driver output: $(printf '%s' "$INSTALL_OUT" | tr '\n' '|')"
printf '%s' "$INSTALL_OUT" | grep -q "DRIVER_INSTALL_OK" \
    || fail "B: install driver did not reach an active bundle commit"
pass "B committed bundletest as active with an owned bundletest.bt registry entry"

ARTIFACT_ID="$(printf '%s' "$INSTALL_OUT" | awk '/^ARTIFACT_ID /{print $2}')"
[ -n "$ARTIFACT_ID" ] || fail "B: could not recover the published artifact_id from driver output"

# The materialized operational files landed on disk (roles overlay).
MSYS_NO_PATHCONV=1 docker exec "$NAME" test -f /config/agents/specialists/bundletest/character.yaml \
    || fail "B: bundletest operational files were not materialized"

# ============================================================
# C: reload the RUNNING server's agents scope — same dispatch as
# casa_reload(scope="agents") — and confirm bundletest resolves active.
# ============================================================
log "C: casa_reload(scope=agents) via /admin/reload"
RELOAD_OUT="$(curl_sock /admin/reload '{"scope":"agents"}')"
log "C reload result: $RELOAD_OUT"
[ "$(json_get "$RELOAD_OUT" 'd.get("status")')" = "ok" ] \
    || fail "C: reload did not report ok: $RELOAD_OUT"
if printf '%s' "$RELOAD_OUT" | grep -q "added_specialist_bundletest"; then
    pass "C reload reported added_specialist_bundletest"
else
    log "C note: 'added_specialist_bundletest' not in actions (status query is authoritative)"
fi

AFTER_STATUS="$(curl_sock /admin/specialist/status '{"slug":"bundletest"}')"
log "C post-reload status: $AFTER_STATUS"
[ "$(json_get "$AFTER_STATUS" 'd.get("state")')" = "active" ] \
    || fail "C: bundletest not active after reload: $AFTER_STATUS"
[ "$(json_get "$AFTER_STATUS" 'd.get("stable_agent_id")')" = "specialist:bundletest" ] \
    || fail "C: unexpected stable_agent_id: $AFTER_STATUS"
pass "C bundletest is a delegatable active specialist after reload"

# Registry-on-disk assertions (the file the running server itself reads).
assert_file_contains "$NAME" /config/plugins/registry.json "bundletest.bt" \
    "C registry.json contains the owned bundletest.bt entry"
assert_file_contains "$NAME" /config/plugins/registry.json "operator-survivor-tool" \
    "C registry.json contains the pre-seeded operator-owned survivor"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    test -d "/config/plugins/store/bundletest.bt/$ARTIFACT_ID" \
    || fail "C: published artifact directory missing from the store"

# ============================================================
# D: uninstall cascade — owned entry removed, ack retired, artifact
# RETAINED on disk (spec §4.4 GC-root policy — no sweep runs here), operator
# survivor untouched, specialist tree gone.
# ============================================================
log "D: specialist_uninstall(bundle=True) cascade"
UNINSTALL_OUT="$(MSYS_NO_PATHCONV=1 docker exec -i "$NAME" python3 - <<'PY'
import sys
sys.path.insert(0, "/opt/casa")

import plugin_registry
from specialist_install import uninstall_specialist
from specialist_install_consent import SpecialistInstallAckStore
import specialist_bundle_journal

acks = SpecialistInstallAckStore()
assert acks.snapshot_slug("bundletest"), "expected a recorded consent ack for bundletest pre-uninstall"

txn = uninstall_specialist(slug="bundletest", bundle=True, acks=acks)
specialist_bundle_journal.complete(txn.journal_path)

reg = plugin_registry.load_registry(plugin_registry.REGISTRY_PATH)
owned = plugin_registry.owned_entries_for("bundletest", reg)
assert owned == [], owned
names = {e["name"] for e in reg.entries}
assert "operator-survivor-tool" in names, names
assert "bundletest.bt" not in names, names
assert acks.snapshot_slug("bundletest") == [], "bundletest consent acks were not retired"

print("REMOVED_ARTIFACT_IDS", ",".join(txn.removed_artifact_ids))
print("DRIVER_UNINSTALL_OK")
PY
)"
log "D driver output: $(printf '%s' "$UNINSTALL_OUT" | tr '\n' '|')"
printf '%s' "$UNINSTALL_OUT" | grep -q "DRIVER_UNINSTALL_OK" \
    || fail "D: uninstall driver did not complete the bundle cascade"
pass "D uninstall cascade removed the owned entry, retired the ack, kept the survivor"

printf '%s' "$UNINSTALL_OUT" | grep -q "REMOVED_ARTIFACT_IDS $ARTIFACT_ID" \
    || fail "D: uninstall txn did not report the expected removed artifact id"

MSYS_NO_PATHCONV=1 docker exec "$NAME" test ! -e /config/specialists/bundletest \
    || fail "D: bundletest's specialist tree was not removed"
MSYS_NO_PATHCONV=1 docker exec "$NAME" \
    test -d "/config/plugins/store/bundletest.bt/$ARTIFACT_ID" \
    || fail "D: owned artifact bytes were NOT retained on disk after uninstall"
pass "D owned artifact bytes retained on disk (GC-root policy — no sweep)"

assert_file_contains "$NAME" /config/plugins/registry.json "operator-survivor-tool" \
    "D registry.json still contains the operator-owned survivor"

# ============================================================
# E: reload again — the running server drops bundletest back to unknown.
# ============================================================
log "E: casa_reload(scope=agents) after uninstall"
RELOAD_OUT2="$(curl_sock /admin/reload '{"scope":"agents"}')"
[ "$(json_get "$RELOAD_OUT2" 'd.get("status")')" = "ok" ] \
    || fail "E: post-uninstall reload did not report ok: $RELOAD_OUT2"
if printf '%s' "$RELOAD_OUT2" | grep -q "evicted_specialist_bundletest"; then
    pass "E reload reported evicted_specialist_bundletest"
else
    log "E note: 'evicted_specialist_bundletest' not in actions (status query is authoritative)"
fi

FINAL_STATUS="$(curl_sock_raw /admin/specialist/status '{"slug":"bundletest"}')"
log "E post-uninstall status: $FINAL_STATUS"
[ "$(json_get "$FINAL_STATUS" 'd.get("state")')" = "not_installed" ] \
    || fail "E: bundletest still known after uninstall+reload: $FINAL_STATUS"
pass "E bundletest is unknown again after uninstall + reload"

pass "ALL PASS — bundled specialist one-flow install + uninstall cascade"
